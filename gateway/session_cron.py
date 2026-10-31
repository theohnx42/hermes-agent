"""Trusted cron producer and owner execution of the existing scheduler contract.

Public callers name a persisted job, never supply agent policy or arbitrary jobs.
The ordinary admission ledger owns execution; the scheduler owns final delivery.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import threading

from gateway.session_contract import Principal, SessionRef
from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admission

_execution: ContextVar[tuple | None] = ContextVar('cron_owner_execution', default=None)
_owners = {}


def bind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    _owners[home] = (os.getpid(), authority, asyncio.get_running_loop())


def unbind_owner(authority):
    home = Path(authority.db.db_path).resolve().parent
    bound = _owners.get(home)
    if bound is not None and bound[1] is authority:
        del _owners[home]


def owner_for_home(home):
    bound = _owners.get(Path(home).resolve())
    if bound is not None and bound[0] == os.getpid() and not bound[2].is_closed():
        return bound[1:]
    return None


def current_execution():
    return _execution.get()


def bind_execution(authority, session_id, job_id, execution_id):
    """Bind this context's cron admission ``(authority, session_id, job_id, execution_id)``, the
    layout ``current_execution()`` readers index; returns the ``ContextVar`` reset token."""
    return _execution.set((authority, session_id, job_id, execution_id))


def _actor(authority, actor):
    if actor is None:
        return Principal(CRON_PRINCIPAL_ID, authority.profile_id,
                         frozenset({'session:create', 'session:submit', 'session:read', 'session:control'}), 'cron-ticker')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    if not {'session:create', 'session:submit', 'session:control'} <= actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    # Cron fires belong to the profile scheduler, not the transport that observes
    # them. The same execution ID from ticker and CLI must dedupe together.
    return Principal(CRON_PRINCIPAL_ID, authority.profile_id, actor.capabilities, actor.transport_id)


def _validate_params(params):
    keys = set(params)
    if keys not in ({'job_id', 'request_id', 'extra_prompt'},
                    {'job_id', 'request_id', 'extra_prompt', 'store_profile'}):
        raise RuntimeStoreError('invalid_params')
    if (any(not isinstance(params[k], str) or not params[k] for k in ('job_id', 'request_id'))
            or len(params['request_id']) > 256
            or (params['extra_prompt'] is not None and not isinstance(params['extra_prompt'], str))):
        raise RuntimeStoreError('invalid_params')
    if 'store_profile' in params:
        from cron.jobs import normalize_execution_profile
        try:
            if normalize_execution_profile(params['store_profile']) != params['store_profile']:
                raise ValueError('noncanonical store profile')
        except (ValueError, TypeError):
            raise RuntimeStoreError('invalid_params') from None


def _bound_job(authority, params):
    """A target owner may execute only the persisted assignment of a live source claim."""
    from cron.jobs import get_job, use_cron_store, execution_profile_home
    if 'store_profile' not in params:
        job = get_job(params['job_id'])
        if job and job.get('execution_profile'):
            raise RuntimeStoreError('invalid_params')  # every explicit assignment needs its source claim
        return job
    store = execution_profile_home(params['store_profile'])
    from gateway.session_authorities import authority_for_home, require_serving
    source = authority_for_home(authority.runner, store)
    if source is None:
        raise RuntimeStoreError('profile_mismatch')
    require_serving(source)
    with use_cron_store(store):
        job = get_job(params['job_id'])
        if (job is None or not job.get('execution_profile')
                or execution_profile_home(job['execution_profile']) != Path(authority.profile_id).resolve()
                or not (store / 'cron' / 'executions.db').is_file()):
            raise RuntimeStoreError('profile_mismatch')
        from gateway.session_authorities import local_bots
        target = next((bot for bot in local_bots(authority.runner)
                       if bot.authority is authority and bot.name == job['execution_profile']), None)
        if target is None or target.problem or target.retiring:
            raise RuntimeStoreError('profile_mismatch')
        from cron.executions import get_execution, execution_owner_verified
        row = get_execution(params['request_id'])
        if (row is None or row['job_id'] != job['id'] or row['status'] not in {'claimed', 'running'}
                or not execution_owner_verified(params['request_id'], job['id'])):
            raise RuntimeStoreError('cron_claim_unverified')
        return job


def _create(authority, actor, params):
    from gateway.session_local_recovery import local_identity, restore_local_session
    from cron.jobs import execution_profile_home
    from gateway.session_policy import build_policy, bind_launch_key
    from gateway.run import _load_gateway_config
    from cron.scheduler import _load_cron_job_config
    from gateway.config import Platform
    from gateway.session import SessionSource, SessionEntry
    from gateway.session_lifecycle import _now

    _validate_params(params)
    jid, request_id, extra = params['job_id'], params['request_id'], params['extra_prompt']
    execution_id, request_id = request_id, cron_request_id(jid, request_id)
    sid = local_identity(authority.profile_id, actor.subject, request_id)
    if authority.db.get_session(sid) is not None:
        ref = restore_local_session(authority, sid)
        from gateway.session_local_recovery import local_adapter_map
        frozen = json.loads(local_adapter_map(authority)[Platform.LOCAL].policies[sid].request_json)
        if (frozen.get('store_profile') != params.get('store_profile')
                or frozen['cron_job']['id'] != jid or frozen['extra_prompt'] != extra):
            raise RuntimeStoreError('admission_conflict')
        return ref, request_id
    if ':' in execution_id:
        # Fresh admissions only: ``cron:<job>:<execution>`` then splits at its one ``:`` after the
        # job. Ledger ids are hex, so only a hand-made id has one; an identity admitted before this
        # guard keeps its exact retry above, and run history files it by its stored policy.
        raise RuntimeStoreError('invalid_params')
    job = _bound_job(authority, params)
    if job is None or job.get('no_agent'):
        raise RuntimeStoreError('not_found')
    config = _load_gateway_config()
    private = {}
    launch = {'source': 'cli', 'request_id': request_id, 'toolsets': []}
    if job.get('workdir'):
        launch['cwd'] = job['workdir']
    policy = build_policy(launch, config, private_secrets=private)
    policy = replace(policy, source='cron', platform='cron', model=_load_cron_job_config(job, jid, job.get('name') or jid).model,
                     request_json=json.dumps({'cron_job': job, 'extra_prompt': extra, 'request_id': request_id,
                                              **({'store_profile': params['store_profile'],
                                                  'execution_profile': job['execution_profile'],
                                                  'store_home': str(execution_profile_home(params['store_profile'])),
                                                  'execution_home': str(Path(authority.db.db_path).resolve().parent)}
                                                 if 'store_profile' in params else {})}))
    policy = bind_launch_key(authority, sid, policy, None, config_secrets=private)
    from gateway.session_local_recovery import local_source
    source = local_source(authority, sid, actor.subject)
    route = authority.runner.session_store._generate_session_key(source)
    now = _now()
    entry = SessionEntry(route, sid, now, now, origin=source, platform=Platform.LOCAL)
    # The staged creation receipt: ``operation('submit')`` commits it off the owner loop.
    return {'profile_id': authority.profile_id, 'principal_id': actor.subject, 'request_id': request_id,
            'session_id': sid, 'route': route, 'entry': entry.to_dict(), 'policy': asdict(policy)}, request_id


# What a message typed into a scheduled job's session settles with (see ``execute``); its text is
# ``gateway.errors.cron_session_read_only``, rendered in the owning profile's language.
CRON_SESSION_READ_ONLY_SURFACE = {'layer': 'gateway', 'code': 'cron_session_read_only', 'retryable': False}

# Run-side verdicts run_job stamps on the job dict for the firing scheduler's bookkeeping tail
# (cron/quota_hold.py parks past a closed usage window; cron/unreachable_retry.py re-runs a
# never-reached model). They cross the authority boundary inside the cron result.
RUN_JOB_FLAGS = ('_model_unreachable', '_quota_hold_seconds')


async def operation(authority, name, params, actor=None):
    actor = _actor(authority, actor)
    from gateway.session_authorities import require_serving
    require_serving(authority)  # released ownership: nothing here is ours to touch
    # Only new work is fenced by a drain. ``recover``/``status``/``cancel`` observe or stop work
    # already admitted, which the drain is waiting for: refusing them makes the firer give up
    # (CronExecutionUnknown pauses the job) on every restart that overlaps a cron run.
    if name == 'recover':
        from gateway.session_local_recovery import local_identity
        _validate_params(params)
        request_id = cron_request_id(params['job_id'], params['request_id'])
        sid = local_identity(authority.profile_id, actor.subject, request_id)
        from hermes_state_runtime import list_session_admissions
        rows = list_session_admissions(authority.db, session_id=sid, pending_only=False)
        row = next((r for r in rows if r['request_id'] == request_id and r['principal_id'] == actor.subject), None)
        if row is None:
            return {'status': 'missing', 'result': None}
        state = await operation(authority, 'status', {'session_id': sid, 'admission_id': row['admission_id']}, actor)
        from gateway.session_local_recovery import restore_local_session
        restore_local_session(authority, sid)
        from gateway.config import Platform
        from gateway.session_local_recovery import local_adapter_map
        policy = local_adapter_map(authority)[Platform.LOCAL].policies[sid]
        frozen = json.loads(policy.request_json)
        if (frozen['extra_prompt'] != params['extra_prompt']
                or frozen.get('store_profile') != params.get('store_profile')):
            raise RuntimeStoreError('admission_conflict')
        state['job'] = frozen['cron_job']
        # The receipt a firer whose submit answer was lost polls, exactly as if it had arrived.
        state.update(session_id=sid, admission_id=row['admission_id'])
        return state
    if name == 'submit':
        authority._require_admission_open()
        from gateway.session_authorities import owner_scope
        # The job id, jobs file and gateway config belong to the OWNING profile.
        with owner_scope(authority):
            ref, request_id = _create(authority, actor, params)
        from functools import partial
        from gateway.session_runtime_workers import tracked_write
        if isinstance(ref, dict):
            # The creation commit waits on the writer off the owner loop, like session.create (N50).
            from gateway.session_local_recovery import restore_local_session
            from hermes_state_local import commit_local_session
            await tracked_write(authority, partial(commit_local_session, authority.db, epoch=authority.epoch, receipt=ref))
            ref = restore_local_session(authority, ref['session_id'])
        receipt = await tracked_write(authority, partial(
            admit_session_input, authority.db, epoch=authority.epoch, principal_id=actor.subject,
            session_id=ref.session_id, request_id=request_id, payload={'text': params['extra_prompt'] or ''},
            _authorize_write=authority._admission_gate()), then=partial(authority._admitted, ref, None), ordered=True,
            after=partial(authority._schedule_admitted, ref))
        return {'session_id': ref.session_id, 'admission_id': receipt.admission_id}
    if set(params) != {'session_id', 'admission_id'} or name not in {'status', 'cancel'}:
        raise RuntimeStoreError('invalid_params')
    ref = SessionRef(authority.profile_id, params['session_id'])
    authority.authorize(actor, ref, 'session:submit')
    row = get_session_admission(authority.db, admission_id=params['admission_id'])
    if row is None or row['target_session_id'] != ref.session_id or row['principal_id'] != actor.subject:
        raise RuntimeStoreError('permission_denied')
    if name == 'cancel':
        if row['status'] == 'queued':
            row = await authority.cancel_or_claimed(actor, ref, row['admission_id']) or {'status': 'terminal'}
        if row['status'] == 'started':
            # The claim commits before execute() registers its event; latching the
            # cancellation on the admission here means that window cannot lose it.
            _cancellations(authority).setdefault(row['admission_id'], threading.Event()).set()
        return {'ok': True}
    result, flags, binding = None, {}, None
    if row['status'] == 'terminal':
        from gateway.session_results import admission_result
        saved = admission_result(authority.db, row['admission_id'])
        result = saved['result'].get('cron_result') if saved else None
        flags = (saved['result'].get('cron_job_flags') or {}) if saved else {}
        binding = saved['result'].get('cron_execution_binding') if saved else None
        if result is None:
            result = [False, '', '', row.get('outcome') or 'unknown_execution']
    from cron.scheduler_liveness import owner_progress
    return {'status': row['status'], 'result': result, 'job_flags': flags,
            **({'execution_binding': binding} if binding else {}),
            'progress_at': owner_progress.get(row['request_id'].rsplit(':', 1)[-1])}


def _cancellations(authority):
    cancellations = getattr(authority, '_cron_cancellations', None)
    if cancellations is None:
        cancellations = authority._cron_cancellations = {}
    return cancellations


async def rpc(connection, name, params):
    return await operation(connection.authority, name, params, connection.actor)


def scheduler_execution_id(job_id, request_id):
    """The scheduler's execution id this admission carries: ``_create`` admits under
    ``cron:<job>:<request_id>`` and the ticker's ``request_id`` IS its ``cron/executions`` row id."""
    return request_id.removeprefix(cron_request_id(job_id, ''))


def _run_identified(run_job, job, execution_id, *, require_live_claim=False, **kwargs):
    """``run_job`` inside this fire's cron identity (``ctx.current_cron_execution()``).

    The scheduler binds it in the FIRING thread, but the owner executes on its own loop, whose
    context never saw that binding; re-bind it here from the same ledger row, in the profile scope
    the caller entered (so the row and the profile name are this profile's)."""
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution, execution_owner_verified
    if require_live_claim and not execution_owner_verified(execution_id, job['id']):
        raise RuntimeStoreError('cron_claim_unverified')
    token = enter_cron_execution(job, execution_id, get_execution(execution_id) or {})
    try:
        return run_job(job, execution_id=execution_id, **kwargs)
    finally:
        exit_cron_execution(token)


async def execute(authority, ref, row, policy):
    from cron.scheduler import run_job
    from gateway.run import _profile_runtime_scope
    data = json.loads(policy.request_json)
    if (row['payload'] != {'text': data['extra_prompt'] or ''}
            or row['request_id'] != data['request_id']
            or row['principal_id'] != authority.sessions[ref.session_id].source.user_id):
        # A cron session only runs its own frozen fire; anything else typed into it fails with a
        # descriptor the viewer can render (and offer a new chat for), never a generic failure.
        from agent.i18n import t
        text = t('gateway.errors.cron_session_read_only')
        authority.pending_results[row['admission_id']] = {'result': {
            'final_response': '', 'messages': [], 'failed': True, 'completed': False,
            'error': text, 'error_surface': dict(CRON_SESSION_READ_ONLY_SURFACE)},
            'usage': {}}
        return text
    cancellations = _cancellations(authority)
    cancel = cancellations.setdefault(row['admission_id'], threading.Event())
    execution_id = scheduler_execution_id(data['cron_job']['id'], row['request_id'])
    # The frozen snapshot predates the fire's ledger row; the run's progress stamps need its id.
    job = dict(data['cron_job'], execution_id=execution_id)
    token = bind_execution(authority, ref.session_id, job['id'], execution_id)
    try:
        db_path = await asyncio.to_thread(Path(authority.db.db_path).resolve)
        from cron.jobs import use_cron_store, execution_profile_home
        store = (execution_profile_home(data['store_profile'])
                 if 'store_profile' in data else db_path.parent)
        if ('execution_profile' in data
                and (execution_profile_home(data['execution_profile']) != db_path.parent
                     or data.get('execution_home') != str(db_path.parent)
                     or data.get('store_home') != str(store))):
            raise RuntimeStoreError('profile_mismatch')
        with use_cron_store(store), _profile_runtime_scope(db_path.parent):
            result = await asyncio.to_thread(_run_identified, run_job, job, execution_id,
                                             require_live_claim='store_profile' in data,
                                             extra_prompt=data['extra_prompt'], cancel_event=cancel)
        # run_job stamps run-side verdicts on ITS job copy (quota hold, unreachable model); the
        # firing scheduler's bookkeeping reads them off its own dict, so they ride the result.
        flags = {key: job[key] for key in RUN_JOB_FLAGS if key in job}
        binding = ({key: data[key] for key in ('store_profile', 'store_home', 'execution_profile', 'execution_home')}
                   if 'store_profile' in data else {})
        authority.pending_results[row['admission_id']] = {
            'result': {'cron_execution_binding': binding, 'final_response': result[2], 'cron_result': list(result), 'cron_job_flags': flags,
                       'failed': not result[0], 'completed': result[0]}, 'usage': {}}
        if not result[0]:
            raise RuntimeError(result[3] or 'cron execution failed')
        return result[2]
    finally:
        from cron.scheduler_liveness import owner_progress
        owner_progress.pop(execution_id, None)
        _execution.reset(token)
        cancellations.pop(row['admission_id'], None)
