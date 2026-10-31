"""One schedule/store can explicitly execute in another existing profile, without moving history."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def network_ports(monkeypatch):
    """Only explicitly started fixture providers may receive traffic; never operator services."""
    import socket
    allowed = set()
    connect, connect_ex, lookup = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def check(address):
        if (not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1', 'localhost'}
                or address[1] not in allowed):
            raise AssertionError('test forbids non-fixture socket connection')
    def guarded_connect(sock, address):
        check(address)
        return connect(sock, address)
    def guarded_connect_ex(sock, address):
        check(address)
        return connect_ex(sock, address)
    def guarded_lookup(host, port, *args, **kwargs):
        if host not in {'127.0.0.1', '::1', 'localhost', b'127.0.0.1', b'::1', b'localhost'}:
            raise AssertionError('test forbids external DNS')
        return lookup(host, port, *args, **kwargs)
    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded_lookup)
    return allowed


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    ghost = root / 'profiles' / 'ghost'
    ghost.mkdir(parents=True)
    (root / 'config.yaml').write_text('model:\n  default: root-model\n')
    (ghost / 'config.yaml').write_text('model:\n  default: ghost-model\n')
    (root / 'SOUL.md').write_text('root identity')
    (ghost / 'SOUL.md').write_text('ghost identity')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.delenv('HERMES_CRON_STORE_BINDING', raising=False)
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS', '1')
    from cron import jobs, executions, notepad, incidents
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', None)
    monkeypatch.setattr(incidents, 'EXECUTIONS_FILE', None)
    monkeypatch.setattr(notepad, 'NOTEPAD_FILE', None)
    with jobs.use_cron_store(root):
        yield root, ghost


def persisted_job(homes, *, target='ghost'):
    from cron import jobs
    job = {'id': 'one-job', 'name': 'one-job', 'prompt': 'read only', 'enabled': True,
           'state': 'scheduled', 'schedule': {'kind': 'cron', 'expr': '0 5 * * *'},
           'next_run_at': '2099-01-01T05:00:00+00:00'}
    if target is not None:
        job['execution_profile'] = target
    jobs.save_jobs([job])
    return job


def live_claim(job):
    from cron import executions
    row = executions.create_execution(job['id'], source='direct')
    assert executions.mark_execution_running(row['id']) is not None
    return row['id']


def authorities(homes):
    from gateway.session_authorities import SessionAuthorities
    root, ghost = homes
    registry = SessionAuthorities(root, multiplexed=True)
    runner = SimpleNamespace(session_authorities=registry)
    source = SimpleNamespace(profile_id=str(root), runner=runner)
    target = SimpleNamespace(profile_id=str(ghost), runner=runner)
    registry.add(root, source)
    registry.add(ghost, target, name='ghost')
    return source, target


def test_real_central_ledger_and_notepad_do_not_follow_executor_home(homes):
    from cron import jobs, executions, notepad, incidents
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    root, ghost = homes
    job = persisted_job(homes)
    eid = live_claim(job)
    token = set_hermes_home_override(str(ghost))
    try:
        assert jobs.get_cron_store_home() == root
        assert executions.get_execution(eid)['job_id'] == job['id']
        notepad.set_note(job['id'], 'cursor', 'accepted-head')
        assert incidents._db_path() == root / 'cron' / 'executions.db'
        assert jobs.get_cron_output_dir() == root / 'cron' / 'output'
    finally:
        reset_hermes_home_override(token)
    assert notepad.get_note(job['id'], 'cursor') == 'accepted-head'
    assert not (ghost / 'cron' / 'executions.db').exists()
    assert not (ghost / 'cron' / 'notepad.db').exists()


@pytest.mark.parametrize('value', ['../ghost', '/tmp/ghost', 'missing', False, ['ghost']])
def test_assignment_rejects_invalid_or_missing_canonical_profile(homes, value):
    from cron.jobs import normalize_execution_profile
    with pytest.raises((ValueError, TypeError)):
        normalize_execution_profile(value)


def test_persisted_binding_and_live_claim_required_before_target_admission(homes):
    from cron import jobs, executions
    from gateway.session_cron import _bound_job
    from hermes_state_runtime import RuntimeStoreError
    job = persisted_job(homes)
    source, target = authorities(homes)
    eid = live_claim(job)
    params = {'job_id': job['id'], 'request_id': eid, 'extra_prompt': None, 'store_profile': 'default'}
    assert _bound_job(target, params)['execution_profile'] == 'ghost'
    with pytest.raises(RuntimeStoreError):
        _bound_job(source, params)
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, dict(params, request_id='absent'))
    executions.finish_execution(eid, success=True)
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, params)
    jobs.update_job(job['id'], {'execution_profile': ''})
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, params)


def test_unbound_rpc_cannot_discard_persisted_executor(homes):
    from gateway.session_cron import _bound_job
    from hermes_state_runtime import RuntimeStoreError
    job = persisted_job(homes)
    source, _ = authorities(homes)
    with pytest.raises(RuntimeStoreError):
        _bound_job(source, {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None})


def test_auth_profile_and_capability_guards_are_unchanged(homes):
    from gateway.session_cron import _actor
    from gateway.session_contract import Principal
    from hermes_state_runtime import RuntimeStoreError
    source, target = authorities(homes)
    with pytest.raises(RuntimeStoreError):
        _actor(target, Principal('caller', source.profile_id, frozenset({'session:create','session:submit','session:control'}), 'rpc'))
    with pytest.raises(RuntimeStoreError):
        _actor(target, Principal('caller', target.profile_id, frozenset({'session:read'}), 'rpc'))


def test_concurrent_runtime_home_does_not_cross_store_scopes(homes):
    from cron import jobs, notepad
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
    root, ghost = homes
    before = dict(os.environ)
    def run(runtime, store, value):
        token = set_hermes_home_override(str(runtime))
        try:
            with jobs.use_cron_store(store):
                notepad.set_note('job', 'owner', value)
                return get_hermes_home(), jobs.get_cron_store_home(), notepad.get_note('job', 'owner')
        finally:
            reset_hermes_home_override(token)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run, ghost, root, 'central')
        second = pool.submit(run, root, ghost, 'sibling')
        assert first.result() == (ghost, root, 'central')
        assert second.result() == (root, ghost, 'sibling')
    assert dict(os.environ) == before


def test_non_native_execution_identity_cannot_mint_or_forge_store_context(homes, monkeypatch):
    from cron import jobs, notepad
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    root, ghost = homes
    job = persisted_job(homes)
    eid = live_claim(job)
    home_token = set_hermes_home_override(str(ghost))
    token = enter_cron_execution(job, eid, get_execution(eid))
    try:
        with pytest.raises(ValueError, match="native cron execution context unavailable"):
            jobs.cron_child_store_binding()
        forged = {"version": 1, "store_profile": "default", "store_home": str(root),
                  "execution_profile": "ghost", "execution_home": str(ghost),
                  "job_id": job['id'], "execution_id": eid, "session_id": "not-admitted"}
        monkeypatch.setenv('HERMES_CRON_STORE_BINDING', json.dumps(forged))
        with jobs.use_cron_store(ghost):
            with pytest.raises(ValueError):
                with jobs.child_notepad_store(job['id']):
                    pytest.fail('unregistered native identity exposed central notepad')
    finally:
        exit_cron_execution(token)
        reset_hermes_home_override(home_token)
    assert notepad.get_note(job['id'], 'cursor') is None
    assert not (ghost / 'state.db').exists()
    assert not (ghost / 'cron' / 'notepad.db').exists()


def test_recovery_uses_frozen_executor_after_assignment_edit(homes, monkeypatch):
    from cron import scheduler_authority as transport, jobs
    from gateway import session_cron
    root, ghost = homes
    job = persisted_job(homes)
    journal = transport.journal_path(job['id'], 'same-fire')
    journal.parent.mkdir(parents=True)
    params = {'job_id': job['id'], 'request_id': 'same-fire', 'extra_prompt': None, 'store_profile': 'default'}
    journal.write_text(json.dumps({'params': params, 'receipt': None, 'execution_profile': 'ghost', 'execution_home': str(ghost), 'store_home': str(root)}))
    jobs.update_job(job['id'], {'execution_profile': ''})
    observed = []
    @asynccontextmanager
    async def peer(home, *, bound):
        observed.append((home, bound))
        class Client:
            async def rpc(self, name, **values):
                assert values == params or set(values) == {'session_id','admission_id'}
                if name == 'cron.submit':
                    return {'session_id': 'native-ghost', 'admission_id': 'one'}
                return {'status': 'terminal', 'result': [True, 'doc', 'answer', None]}
        yield Client()
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    monkeypatch.setattr(transport, '_connect_owner', peer)
    assert transport.run_canonical_job(job, execution_id='same-fire') == (True, 'doc', 'answer', None)
    assert observed == [(ghost, True)]
    assert json.loads(journal.read_text())['execution_profile'] == 'ghost'


def test_bound_missing_owner_never_ensures_or_starts_gateway(homes, monkeypatch):
    from cron.scheduler_authority import _connect_owner
    from hermes_cli import gateway_runtime, gateway_client
    _, ghost = homes
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', lambda home: gateway_runtime.GatewayDiscovery('absent'))
    monkeypatch.setattr(gateway_runtime, 'ensure_gateway_runtime', lambda *a, **k: pytest.fail('new gateway forbidden'))
    async def check():
        with pytest.raises(gateway_client.GatewayUnavailableError):
            async with _connect_owner(ghost, bound=True):
                pytest.fail('absent target attached')
    asyncio.run(check())


def test_executor_home_rotation_fails_closed(homes):
    from cron.scheduler_authority import _record_executor, CronExecutionUnknown
    root, ghost = homes
    with pytest.raises(CronExecutionUnknown):
        _record_executor({'execution_profile': 'ghost', 'execution_home': str(root / 'elsewhere'), 'store_home': str(root)}, root)
    assert _record_executor({'execution_profile': 'ghost', 'execution_home': str(ghost), 'store_home': str(root)}, root) == ghost
    assert _record_executor({}, root) == root


def test_lost_bound_submission_remains_unknown_without_second_submit(homes, monkeypatch):
    from cron import scheduler_authority as transport
    from gateway import session_cron
    from hermes_cli import gateway_client
    job = persisted_job(homes)
    calls = []
    @asynccontextmanager
    async def peer(home, *, bound):
        class Client:
            async def rpc(self, method, **params):
                calls.append(method)
                raise gateway_client.GatewayClientError('disconnected')
        yield Client()
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    monkeypatch.setattr(transport, '_connect_owner', peer)
    with pytest.raises(transport.CronExecutionUnknown):
        transport.run_canonical_job(job, execution_id='unknown-fire')
    assert calls == ['cron.submit']
    saved = json.loads(transport.journal_path(job['id'], 'unknown-fire').read_text())
    assert saved['execution_profile'] == 'ghost'
    assert saved['params']['store_profile'] == 'default'
    assert saved['store_home'] == str(homes[0])
    assert saved['receipt'] is None


def test_default_store_accessor_follows_runtime_without_explicit_scope(tmp_path, monkeypatch):
    from cron import jobs
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(tmp_path))
    try:
        assert jobs.get_cron_store_home() == tmp_path
    finally:
        reset_hermes_home_override(token)


def _native_fixture_owners(root, ghost, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import SessionAuthority
    from hermes_state_registry import acquire
    from hermes_state_runtime import begin_runtime_epoch
    config = GatewayConfig()
    config.multiplex_profiles = True
    runner = SimpleNamespace(_draining=False, config=config,
                             session_store=SessionStore(root / 'sessions', config), adapters={})
    registry = runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    root_db, ghost_db = acquire(root / 'state.db'), acquire(ghost / 'state.db')
    source = SessionAuthority(runner, profile_id=str(root), instance_id='root-test', db=root_db,
                              epoch=begin_runtime_epoch(root_db, instance_id='root-test'))
    target = SessionAuthority(runner, profile_id=str(ghost), instance_id='ghost-test', db=ghost_db,
                              epoch=begin_runtime_epoch(ghost_db, instance_id='ghost-test'))
    registry.add(root, source)
    registry.add(ghost, target, name='ghost')
    ghost_db.create_session('prior-ghost-chat', source='cli', chat_id='local-prior-ghost', user_id='operator-fixture')
    ghost_db.set_session_title('prior-ghost-chat', 'Bot Chat')
    ghost_db.set_session_hidden('prior-ghost-chat', True)
    monkeypatch.setattr(target, '_schedule', lambda ref: None)
    return root_db, ghost_db, target


def _direct_native_notepad_child(root_db, ghost_db, target, sid, job, eid):
    """Actual child-env factory and CLI, separately from the model's read_file trace; no shell/rc."""
    import subprocess
    import sys
    from cron import jobs
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution
    from gateway import session_cron
    from gateway.run import _profile_runtime_scope
    from tools.environments.local import build_subprocess_env
    root, ghost = Path(root_db.db_path).parent, Path(ghost_db.db_path).parent
    native = session_cron.bind_execution(target, sid, job['id'], eid)
    try:
        with jobs.use_cron_store(root), _profile_runtime_scope(ghost, prepared_secret_scope={}):
            token = enter_cron_execution(job, eid, get_execution(eid))
            try:
                env = build_subprocess_env(strip_launch_profile=True)
            finally:
                exit_cron_execution(token)
    finally:
        session_cron._execution.reset(native)
    assert 'HERMES_CRON_STORE_BINDING' in env
    assert env.get('HOME') == os.environ.get('HOME')
    child = ('import runpy,sys\n'
             'def audit(event,args):\n'
             ' if event in ("socket.connect","socket.getaddrinfo"): raise RuntimeError("notepad fixture forbids networking")\n'
             'sys.addaudithook(audit)\n'
             'sys.argv=sys.argv[1:]\n'
             'runpy.run_module("hermes_cli.main",run_name="__main__")\n')
    source_root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, '-c', child, 'hermes', 'cron', 'notepad', job['id'],
                             'set', 'cursor', 'ghost-accepted-head'], env=env, cwd=source_root,
                            capture_output=True, text=True, timeout=12, check=False)
    (root / 'notepad-child-stdout.txt').write_text(result.stdout, encoding='utf-8')
    (root / 'notepad-child-stderr.txt').write_text(result.stderr, encoding='utf-8')
    code = result.returncode
    assert code == 0, 'audited private CLI child failed; inspect private notepad-child logs'


async def _execute_native_fixture(target, actor, params, job, eid, root_db, ghost_db, ghost, scenario):
    from cron import jobs, executions
    from gateway import session_cron
    from gateway.config import Platform
    from gateway.session_contract import SessionRef
    from gateway.session_local_recovery import local_adapter_map
    from hermes_state_runtime import claim_session_input, settle_session_input
    receipt = await session_cron.operation(target, 'submit', params, actor)
    assert await session_cron.operation(target, 'submit', params, actor) == receipt
    assert root_db.get_session(receipt['session_id']) is None
    policy = local_adapter_map(target)[Platform.LOCAL].policies[receipt['session_id']]
    frozen = json.loads(policy.request_json)
    assert (frozen['store_profile'], frozen['execution_profile'], policy.model) == ('default', 'ghost', 'ghost-model')
    row = claim_session_input(ghost_db, epoch=target.epoch, session_id=receipt['session_id'])
    assert row and row['status'] == 'started'
    if scenario == 'assignment_edited':
        jobs.update_job(job['id'], {'execution_profile': ''})
    if scenario == 'claim_finished':
        from hermes_state_runtime import RuntimeStoreError
        executions.finish_execution(eid, success=False, error='fixture owner finished')
        with pytest.raises(RuntimeStoreError, match='cron_claim_unverified'):
            await session_cron.execute(target, SessionRef(str(ghost), receipt['session_id']), row, policy)
        return receipt['session_id']
    await asyncio.to_thread(_direct_native_notepad_child, root_db, ghost_db, target, receipt['session_id'], job, eid)
    result = await session_cron.execute(target, SessionRef(str(ghost), receipt['session_id']), row, policy)
    assert result == 'GHOST_BOUND_SUCCESS'
    saved = target.pending_results[row['admission_id']]
    settle_session_input(ghost_db, epoch=target.epoch, admission_id=row['admission_id'],
                         generation=row['generation'], outcome='completed', result=saved)
    status = await session_cron.operation(target, 'status', receipt, actor)
    assert status['status'] == 'terminal' and status['result'][0] is True
    binding = {'store_profile': 'default', 'store_home': str(Path(root_db.db_path).resolve().parent),
               'execution_profile': 'ghost', 'execution_home': str(ghost.resolve())}
    assert status['execution_binding'] == binding
    assert saved['result']['cron_execution_binding'] == binding
    from gateway.session_controls import AuthorityConnection
    from gateway.session_group_controls import dispatch_group_control
    view = AuthorityConnection(target, SimpleNamespace(),
                               {'user_id': 'operator-fixture', 'profile_id': str(ghost),
                                'instance_id': 'ghost-test'}, operator=True)
    roster = await dispatch_group_control(view, 'profiles.list', {'include_sessions': True})
    visible = next(item for item in roster['profiles'] if item['name'] == 'ghost')
    assert visible['canonical_session']['id'] == 'prior-ghost-chat'
    assert visible['last_session']['id'] == receipt['session_id']
    assert visible['last_session']['last_active'] >= visible['canonical_session']['last_active']
    assert visible['worker_session'] is None  # actual cron history, not a fabricated worker timestamp
    return receipt['session_id']



def _assert_role_turn(root_server, ghost_server, ghost_db, root_db, job, eid, sid, scenario):
    from cron import notepad, executions
    assert root_server.main_requests() == []
    requests = ghost_server.main_requests()
    if scenario == 'claim_finished':
        assert requests == []
        assert notepad.get_note(job['id'], 'cursor') is None
        return
    assert len(requests) == 2
    assert all(req['model'] == 'ghost-model' for req in requests)
    ghost_auth_only = all(record['auth'] == 'Bearer fixture-ghost' for record in ghost_server.requests)
    assert ghost_auth_only, 'fixture request did not use the explicit Ghost endpoint key'
    messages = json.dumps(requests[0]['messages'])
    assert 'ghost identity' in messages and 'root identity' not in messages
    assert notepad.get_note(job['id'], 'cursor') == 'ghost-accepted-head'
    assert executions.get_execution(eid)['job_id'] == job['id']
    ghost = Path(ghost_db.db_path).parent
    assert not (ghost / 'cron' / 'executions.db').exists()
    assert not (ghost / 'cron' / 'notepad.db').exists()
    assert ghost_db.get_session(sid) is not None
    rows = ghost_db.list_sessions_rich(source=None, limit=20, order_by_last_active=True, compact_rows=True)
    assert any(row['id'] == sid for row in rows)
    assert root_db.get_session(sid) is None


@pytest.mark.parametrize("scenario", ["live", "assignment_edited", "claim_finished"])
def test_native_ghost_owner_persists_role_turn_and_central_progress(homes, monkeypatch, scenario, network_ports):
    """Real native agent read_file trace; separate no-shell CLI proof and public roster."""
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from cron import jobs
    from gateway.session_contract import Principal
    from hermes_state_registry import release
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
    root, ghost = homes
    with FakeLLMServer([Text('WRONG_ROOT')], api_key='fixture-root') as root_server, FakeLLMServer([
        ToolCall('read_file', {'path': str(ghost / 'SOUL.md')}),
        Text('GHOST_BOUND_SUCCESS'),
    ], api_key='fixture-ghost') as ghost_server:
        network_ports.update({root_server.port, ghost_server.port})
        assert root_server.base_url.startswith('http://127.0.0.1:')
        assert ghost_server.base_url.startswith('http://127.0.0.1:')
        write_hermes_home(root, root_server.base_url, api_key='fixture-root')
        write_hermes_home(ghost, ghost_server.base_url, api_key='fixture-ghost')
        for home, key in ((root, 'fixture-root'), (ghost, 'fixture-ghost')):
            path = home / 'config.yaml'
            path.write_text(path.read_text().replace('  provider: custom\n', f'  provider: custom\n  api_key: {key}\n'))
        path = ghost / 'config.yaml'
        path.write_text(path.read_text().replace('default: fake-model', 'default: ghost-model'))
        monkeypatch.setenv('HERMES_EXEC_ASK', 'never')
        monkeypatch.setenv('HERMES_CRON_TIMEOUT', '20')
        root_db, ghost_db, target = _native_fixture_owners(root, ghost, monkeypatch)
        actor = Principal('operator-fixture', str(ghost),
                          frozenset({'session:create', 'session:submit', 'session:control', 'session:read'}), 'fixture')
        job = persisted_job(homes)
        job['enabled_toolsets'] = ['file']
        jobs.save_jobs([job])
        eid = live_claim(job)
        params = {'job_id': job['id'], 'request_id': eid, 'extra_prompt': None, 'store_profile': 'default'}
        token = set_multiplex_context(True)
        try:
            sid = asyncio.run(_execute_native_fixture(target, actor, params, job, eid, root_db, ghost_db, ghost, scenario))
            _assert_role_turn(root_server, ghost_server, ghost_db, root_db, job, eid, sid, scenario)
            if scenario != 'claim_finished':
                # Exercise the real outer terminal bookkeeping on this actual native result;
                # external delivery remains local/suppressed, not a network-send claim.
                from cron import executions, notepad
                from cron.scheduler import _RunDelivery
                from cron.scheduler_bookkeeping import finish_completed_run
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(ghost, prepared_secret_scope={}):
                    assert finish_completed_run(_RunDelivery(job, True, None), None, eid)
                    assert executions.get_execution(eid)['status'] == 'completed'
                    assert executions.get_execution(eid)['delivery_outcome'] == 'suppressed'
                    assert jobs.get_job(job['id'])['last_status'] == 'ok'
                    assert notepad.get_note(job['id'], 'cursor') == 'ghost-accepted-head'
                assert not (ghost / 'cron' / 'executions.db').exists()
        finally:
            reset_multiplex_context(token)
            release(ghost_db)
            release(root_db)


@pytest.mark.parametrize('override,expected', [(None, 'ghost-model'), ('explicit-pin', 'explicit-pin')])
def test_executor_config_and_existing_model_override_precedence(homes, override, expected):
    from cron.scheduler import _load_cron_job_config
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    _, ghost = homes
    job = persisted_job(homes)
    if override:
        job['model'] = override
    token = set_hermes_home_override(str(ghost))
    try:
        config = _load_cron_job_config(job, job['id'], job['name'])
        assert config.model == expected
        assert (ghost / 'SOUL.md').read_text() == 'ghost identity'
    finally:
        reset_hermes_home_override(token)


def test_forged_source_claim_and_retiring_target_refuse(homes):
    from cron import executions
    from gateway.session_cron import _bound_job
    from hermes_state_runtime import RuntimeStoreError
    job = persisted_job(homes)
    _, target = authorities(homes)
    other = executions.create_execution('another-job', source='direct')['id']
    params = {'job_id': job['id'], 'request_id': other, 'extra_prompt': None, 'store_profile': 'default'}
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, params)
    eid = live_claim(job)
    params['request_id'] = eid
    target.retiring = True
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, params)
    target.retiring = False
    with executions._transaction() as conn:
        conn.execute('UPDATE executions SET pid=?, process_id=? WHERE id=?', (99999999, 'gone-fixture', eid))
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, params)


def test_unknown_foreign_process_fingerprint_is_not_admission_proof(homes, monkeypatch):
    from cron import executions
    from gateway.session_cron import _bound_job
    from hermes_state_runtime import RuntimeStoreError
    job = persisted_job(homes)
    _, target = authorities(homes)
    eid = live_claim(job)
    with executions._transaction() as conn:
        conn.execute('UPDATE executions SET process_id=? WHERE id=?', ('foreign-fixture', eid))
    monkeypatch.setattr(executions, '_process_start_time', lambda pid: None)
    assert executions.execution_owner_live(eid, job['id']) is True  # recovery remains conservative
    assert executions.execution_owner_verified(eid, job['id']) is False
    with pytest.raises(RuntimeStoreError):
        _bound_job(target, {'job_id': job['id'], 'request_id': eid, 'extra_prompt': None, 'store_profile': 'default'})


async def _started_child_binding(root, ghost, target, ghost_db, job, eid):
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    from cron.executions import get_execution
    from cron import jobs
    from gateway import session_cron
    from gateway.run import _profile_runtime_scope
    from hermes_state_runtime import claim_session_input
    params = {'job_id': job['id'], 'request_id': eid, 'extra_prompt': None, 'store_profile': 'default'}
    receipt = await session_cron.operation(target, 'submit', params)
    row = claim_session_input(ghost_db, epoch=target.epoch, session_id=receipt['session_id'])
    assert row['status'] == 'started'
    native = session_cron.bind_execution(target, receipt['session_id'], job['id'], eid)
    try:
        with jobs.use_cron_store(root), _profile_runtime_scope(ghost, prepared_secret_scope={}):
            token = enter_cron_execution(job, eid, get_execution(eid))
            try:
                binding = jobs.cron_child_store_binding()
                assert binding['admission_id'] == row['admission_id']
                assert (binding['generation'], binding['owner_epoch']) == (row['generation'], row['owner_epoch'])
                return binding
            finally:
                exit_cron_execution(token)
    finally:
        session_cron._execution.reset(native)


@pytest.mark.parametrize('changed', ['generation', 'admission_id', 'runtime_epoch'])
def test_real_started_child_binding_refuses_stale_native_identity(homes, monkeypatch, changed):
    """Establish a real positive binding, then isolate each native identity refusal without inference."""
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from cron import jobs, notepad, executions
    from gateway.run import _profile_runtime_scope
    from hermes_state_registry import release
    from hermes_state_runtime import begin_runtime_epoch
    root, ghost = homes
    token = set_multiplex_context(True)
    root_db = ghost_db = None
    try:
        root_db, ghost_db, target = _native_fixture_owners(root, ghost, monkeypatch)
        job = persisted_job(homes)
        eid = live_claim(job)
        binding = asyncio.run(_started_child_binding(root, ghost, target, ghost_db, job, eid))
        monkeypatch.setenv('HERMES_CRON_STORE_BINDING', json.dumps(binding))
        with _profile_runtime_scope(ghost, prepared_secret_scope={}), jobs.use_cron_store(ghost):
            with jobs.child_notepad_store(job['id']):
                notepad.set_note(job['id'], 'cursor', 'accepted')
            if changed == 'runtime_epoch':
                assert begin_runtime_epoch(ghost_db, instance_id='replacement-fixture') != binding['owner_epoch']
            else:
                binding[changed] = binding[changed] + 1 if changed == 'generation' else 'not-the-admitted-id'
                monkeypatch.setenv('HERMES_CRON_STORE_BINDING', json.dumps(binding))
            with pytest.raises(ValueError):
                with jobs.child_notepad_store(job['id']):
                    notepad.set_note(job['id'], 'cursor', 'invalid-write')
        assert notepad.get_note(job['id'], 'cursor') == 'accepted'
        assert executions.execution_owner_verified(eid, job['id']) is True
        assert not (ghost / 'cron' / 'notepad.db').exists()
    finally:
        if ghost_db is not None:
            release(ghost_db)
        if root_db is not None:
            release(root_db)
        reset_multiplex_context(token)


@pytest.mark.parametrize('explicit', [False, True])
def test_loopback_custom_key_contract_does_not_borrow_openai_key(homes, explicit):
    """The custom-host security fence explains the old strict-fixture 401 without contacting a provider."""
    from agent.secret_scope import set_multiplex_context, reset_multiplex_context
    from gateway.run import _profile_runtime_scope
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tests.fakes.fake_llm_provider import write_hermes_home
    _, ghost = homes
    write_hermes_home(ghost, 'http://127.0.0.1:9/v1', api_key='fixture-ghost')
    if explicit:
        path = ghost / 'config.yaml'
        path.write_text(path.read_text().replace('  provider: custom\n', '  provider: custom\n  api_key: fixture-ghost\n'))
    token = set_multiplex_context(True)
    try:
        with _profile_runtime_scope(ghost, prepared_secret_scope={'OPENAI_API_KEY': 'fixture-ghost'}):
            runtime = resolve_runtime_provider(requested='custom', target_model='fake-model')
            # Compare only known synthetic values, never expose an unexpected credential in assertion output.
            correct_fixture_key = runtime.get('api_key') == ('fixture-ghost' if explicit else 'no-key-required')
            assert correct_fixture_key
    finally:
        reset_multiplex_context(token)
