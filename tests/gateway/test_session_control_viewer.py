"""Admission-bound observer proof uses real SQLite without replaying transcript bulk."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway.session_contract import SessionRef
from gateway.session_control_viewer import read_control_proof, restore_before_dispatch, resume_or_controls
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def transcript(tmp_path):
    db = SessionDB(tmp_path / 'viewer.db')
    db.create_session('session', source='telegram')
    yield db, SessionRef('profile', 'session')
    db.close()


def append_turn(db, text='actual admitted input'):
    db.append_message('session', 'user', text, message_uid='actual-user')
    db.append_message('session', 'assistant', tool_calls=[{
        'id': 'actual-call', 'type': 'function',
        'function': {'name': 'computer_use', 'arguments': json.dumps({'action': 'type', 'text': 'fixture'})},
    }], message_uid='actual-call-message')


def test_actual_call_and_user_are_preserved_without_response_bulk(transcript):
    db, ref = transcript
    append_turn(db)
    db.append_message('session', 'tool', 'private-response' * 100000, tool_call_id='actual-call')
    db.append_message('session', 'assistant', 'private-prose' * 100000)
    proof = read_control_proof(db, ref, 0, 'actual admitted input')
    assert [row['message_uid'] for row in proof] == ['actual-user', 'actual-call-message']
    assert proof[1]['tool_calls'][0]['function']['name'] == 'computer_use'
    assert 'private' not in json.dumps(proof)


@pytest.mark.parametrize('anchor', ['different input', '', None])
def test_missing_or_wrong_executed_anchor_is_not_synthesized(transcript, anchor):
    db, ref = transcript
    append_turn(db)
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, 0, anchor)


def test_preclaim_user_tail_is_not_admission_payload_substitute(transcript):
    db, ref = transcript
    mark = db.append_message('session', 'user', 'actual admitted input', message_uid='existing-user')
    db.append_message('session', 'assistant', tool_calls=[{'id': 'call'}])
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, mark, 'actual admitted input')


def test_every_new_user_boundary_remains_visible(transcript):
    db, ref = transcript
    append_turn(db)
    db.append_message('session', 'user', 'intervening input', message_uid='new-boundary')
    proof = read_control_proof(db, ref, 0, 'actual admitted input')
    assert proof[-1] == {'role': 'user', 'message_uid': 'new-boundary', 'content': 'intervening input'}


@pytest.mark.parametrize('oversize', ['user', 'assistant'])
def test_oversize_proof_fails_closed(transcript, oversize):
    db, ref = transcript
    append_turn(db)
    if oversize == 'user':
        db.append_message('session', 'user', 'x' * (128 * 1024 + 1))
    else:
        db.append_message('session', 'assistant', tool_calls=[{'id': 'x' * (128 * 1024)}])
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, 0, 'actual admitted input')


@pytest.mark.parametrize('params', [{'control_only': True}, {'admission_id': 'id'}, {'control_only': None}])
def test_observer_dispatch_never_cold_adopts(params):
    authority = SimpleNamespace(sessions={}, profile_id='profile', retiring=False)
    asyncio.run(restore_before_dispatch(authority, SessionRef('profile', 'missing'), 'session.resume', params))
    assert authority.sessions == {}


@pytest.mark.parametrize('params', [{}, {'control_only': False}])
def test_ordinary_resume_keeps_existing_semantics(params):
    calls = []
    async def ordinary(ref, params):
        calls.append((ref, params))
        return {'ordinary': True}
    connection = SimpleNamespace(resume=ordinary)
    ref = SessionRef('profile', 'session')
    assert asyncio.run(resume_or_controls(connection, ref, params)) == {'ordinary': True}
    assert calls == [(ref, params)]


@pytest.mark.parametrize('race', [None, 'retired', 'settled'])
def test_real_claim_viewer_can_answer_only_same_native_waiter(transcript, monkeypatch, race):
    from dataclasses import replace
    import threading
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import Principal
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
    from tools.approval import resolve_gateway_approval
    from tools.approval_gateway_wait import _await_gateway_decision

    db, ref = transcript
    epoch = begin_runtime_epoch(db, instance_id='viewer-owner')
    admit_session_input(db, epoch=epoch, principal_id='operator', session_id=ref.session_id,
                        request_id='request', payload={'text': 'actual admitted input'})
    row = claim_session_input(db, epoch=epoch, session_id=ref.session_id)
    authority = SessionAuthority(SimpleNamespace(), profile_id=ref.profile_id,
                                 instance_id='viewer-owner', db=db, epoch=epoch)
    live = authority.sessions[ref.session_id] = LiveSession(None, 'private-viewer-test-route')
    authority._stamp_claim(ref, live, authority._claim_with_control_target(ref, lambda: row))
    append_turn(db)
    actor = Principal('operator', ref.profile_id, frozenset({'session:read', 'session:approve'}), 'viewer')
    def forbidden(*args, **kwargs):
        raise AssertionError('control observer entered history or lifecycle path')
    monkeypatch.setattr('gateway.session_local_recovery.prepare_history', forbidden)
    monkeypatch.setattr('gateway.session_local_recovery.local_history', forbidden)
    monkeypatch.setattr('gateway.session_onboarding.keep_from_discard', forbidden)
    monkeypatch.setattr('gateway.session_viewer_close.cancel_persisted_close', forbidden)
    monkeypatch.setattr('gateway.session_deferred_pick.reask_on_attach', forbidden)
    monkeypatch.setattr('tools.async_delegation.allow_for_session', forbidden)
    ready = threading.Event()
    prompt = []

    def notify(data):
        authority.register_approval(ref.session_id, row['generation'], live.route, data)
        prompt.append(data['request_id'])
        ready.set()

    async def exercise():
        task = asyncio.create_task(asyncio.to_thread(_await_gateway_decision, live.route, notify,
            {'command': 'fixture-only', 'allow_session': False, 'allow_permanent': False}))
        try:
            assert await asyncio.to_thread(ready.wait, 5), 'native approval was not published'
            if race:
                from gateway import session_control_viewer
                original = session_control_viewer.read_control_proof
                def changed_owner(*args):
                    proof = original(*args)
                    if race == 'retired':
                        authority.sessions.pop(ref.session_id)
                    else:
                        live.control_claim = None
                        authority.executing.pop(ref.session_id)
                    return proof
                monkeypatch.setattr(session_control_viewer, 'read_control_proof', changed_owner)
                with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
                    await authority.attach(actor, ref, control_only=True, admission_id=row['admission_id'])
                assert not live.subscribers
                return
            snapshot = await authority.attach(actor, ref, control_only=True, admission_id=row['admission_id'])
            assert snapshot.prompts[0]['prompt_id'] == prompt[0]
            assert snapshot.history[0]['content'] == 'actual admitted input'
            with pytest.raises(RuntimeStoreError, match='permission_denied'):
                await authority.respond(replace(actor, transport_id='foreign'), ref, row['generation'],
                                        prompt[0], {'choice': 'once'})
            with pytest.raises(RuntimeStoreError, match='invalid_params'):
                await authority.respond(actor, ref, row['generation'], prompt[0], {'choice': 'always'})
            result = await authority.respond(actor, ref, row['generation'], prompt[0], {'choice': 'deny'})
            assert result['status'] == 'resolved'
            assert (await task)['choice'] == 'deny'
        finally:
            if prompt:
                resolve_gateway_approval(live.route, 'deny', request_id=prompt[0])
            await asyncio.wait_for(task, 5)
    asyncio.run(exercise())


def test_compaction_copies_preserve_logical_identity_and_exclude_old_context(transcript):
    db, ref = transcript
    mark = db.append_message('session', 'user', 'old private context', message_uid='old-user')
    append_turn(db)
    db.create_session('continuation', source='telegram', parent_session_id='session')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='session'"))
    db.append_message('continuation', 'user', 'old private context', message_uid='old-user')
    db.append_message('continuation', 'user', 'actual admitted input', message_uid='actual-user')
    db.append_message('continuation', 'assistant', tool_calls=[{
        'id': 'actual-call', 'type': 'function',
        'function': {'name': 'computer_use', 'arguments': json.dumps({'action': 'type', 'text': 'fixture'})},
    }], message_uid='actual-call-message')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE messages SET active=0 WHERE session_id='session'"))
    proof = read_control_proof(db, ref, mark, 'actual admitted input')
    assert [row['message_uid'] for row in proof] == ['actual-user', 'actual-call-message']


def test_conflicting_logical_identity_fails_closed(transcript):
    db, ref = transcript
    append_turn(db)
    db.append_message('session', 'user', 'conflicting input', message_uid='actual-user')
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, 0, 'actual admitted input')


def test_ambiguous_compression_children_cannot_supply_call_proof(transcript):
    db, ref = transcript
    append_turn(db)
    db.create_session('first-child', source='telegram', parent_session_id='session')
    db.create_session('second-child', source='telegram', parent_session_id='session')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='session'"))
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, 0, 'actual admitted input')


def test_compacted_intervening_user_boundary_cannot_disappear(transcript):
    db, ref = transcript
    append_turn(db)
    boundary = db.append_message('session', 'user', 'later operator input', message_uid='boundary')
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0 WHERE id=?', (boundary,)))
    proof = read_control_proof(db, ref, 0, 'actual admitted input')
    assert proof[-1]['message_uid'] == 'boundary'
    assert proof[-1]['content'] == 'later operator input'


def test_linear_compression_chain_is_not_limited_by_child_fanout(transcript):
    db, ref = transcript
    append_turn(db)
    parent = 'session'
    for index in range(70):
        child = f'continuation-{index}'
        db.create_session(child, source='telegram', parent_session_id=parent)
        db._execute_write(lambda conn, prior=parent: conn.execute(
            "UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id=?", (prior,)))
        parent = child
    proof = read_control_proof(db, ref, 0, 'actual admitted input')
    assert [row['message_uid'] for row in proof] == ['actual-user', 'actual-call-message']


def test_copied_fork_marker_does_not_hide_a_continuation(transcript):
    db, _ = transcript
    append_turn(db)
    db.create_session('continuation', source='telegram', parent_session_id='session',
                      model_config={'_branched_from': 'historical-parent'})
    db.create_session('actual-fork', source='telegram', parent_session_id='session',
                      model_config={'_branched_from': 'session'})
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression', ended_at=1 WHERE id='session'"))
    proof = read_control_proof(db, SessionRef('profile', 'continuation'), 0, 'actual admitted input')
    assert [row['message_uid'] for row in proof] == ['actual-user', 'actual-call-message']


def test_claim_captures_validated_reset_target_before_turn_compression(transcript):
    from gateway.session_authority import LiveSession, SessionAuthority
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_local_migration import LEGACY_PREFIX
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    db, ref = transcript
    db.create_session('reset-target', source='gui', parent_session_id=ref.session_id,
                      model_config={'_reset_from': ref.session_id})
    identity = dict(session_id=ref.session_id, profile_id=ref.profile_id,
                    principal_id='operator', legacy_session_id=ref.session_id)
    receipt = dict(identity, entry={'session_id': 'reset-target'},
                   lineage=[ref.session_id, 'reset-target'])
    def prepare(conn):
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id=?", (ref.session_id,))
        for key, value in [(LEGACY_PREFIX + ref.session_id, identity), (POLICY_PREFIX + ref.session_id, receipt)]:
            conn.execute('INSERT INTO state_meta(key,value) VALUES (?,?)', (key, json.dumps(value)))
    db._execute_write(prepare)
    epoch = begin_runtime_epoch(db, instance_id='reset-owner')
    admit_session_input(db, epoch=epoch, principal_id='operator', session_id=ref.session_id,
                        request_id='reset-request', payload={'text': 'actual admitted input'})
    row = claim_session_input(db, epoch=epoch, session_id=ref.session_id)
    authority = SessionAuthority(SimpleNamespace(), profile_id=ref.profile_id,
                                 instance_id='reset-owner', db=db, epoch=epoch)
    live = authority.sessions[ref.session_id] = LiveSession(None, 'reset-route')
    authority._stamp_claim(ref, live, authority._claim_with_control_target(ref, lambda: row))
    assert live.control_claim['physical_session_id'] == 'reset-target'
    db.append_message('reset-target', 'user', 'actual admitted input', message_uid='reset-user')
    db.create_session('turn-continuation', source='gui', parent_session_id='reset-target')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression',ended_at=1 WHERE id='reset-target'"))
    db.append_message('turn-continuation', 'assistant', tool_calls=[{
        'id': 'reset-call', 'function': {'name': 'computer_use', 'arguments': '{}'}}], message_uid='reset-call-message')
    # The completed local receipt intentionally remains at reset-target during this turn.
    proof = read_control_proof(db, ref, row['transcript_mark'], 'actual admitted input',
                               live.control_claim['physical_session_id'])
    assert [message['message_uid'] for message in proof] == ['reset-user', 'reset-call-message']
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, row['transcript_mark'], 'actual admitted input')
    for seed in ['', 'missing-target', ref.session_id]:
        with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
            read_control_proof(db, ref, row['transcript_mark'], 'actual admitted input', seed)
    # Actual malformed reset metadata reaches the production resolver's dict.get path.
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET model_config='[]' WHERE id='reset-target'"))
    authority._stamp_claim(ref, live, authority._claim_with_control_target(ref, lambda: row))
    assert live.control_claim['physical_session_id'] is None
    assert authority.executing[ref.session_id] == row['generation']


@pytest.mark.parametrize('failure', ['sqlite', 'mapping', 'foreign', 'attribute', 'index'])
def test_optional_target_capture_failure_does_not_poison_ordinary_claim(transcript, monkeypatch, failure):
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_control_viewer import _claim
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    db, ref = transcript
    epoch = begin_runtime_epoch(db, instance_id='optional-owner')
    admit_session_input(db, epoch=epoch, principal_id='operator', session_id=ref.session_id,
                        request_id='optional-request', payload={'text': 'actual admitted input'})
    row = claim_session_input(db, epoch=epoch, session_id=ref.session_id)
    authority = SessionAuthority(SimpleNamespace(), profile_id=ref.profile_id,
                                 instance_id='optional-owner', db=db, epoch=epoch)
    live = authority.sessions[ref.session_id] = LiveSession(None, 'optional-route')
    published = []
    monkeypatch.setattr(authority, '_publish_pending', lambda target: published.append(target))
    def unavailable(conn, session_id):
        if failure == 'sqlite':
            import sqlite3
            raise sqlite3.OperationalError('fixture unavailable')
        if failure == 'mapping':
            raise RuntimeStoreError('storage_unavailable')
        if failure == 'attribute':
            raise AttributeError('malformed mapping fixture')
        if failure == 'index':
            raise IndexError('malformed mapping fixture')
        return 'nonexistent-foreign-target'
    monkeypatch.setattr('hermes_state_local_lineage.local_physical_target', unavailable)
    assert authority._stamp_claim(ref, live, authority._claim_with_control_target(ref, lambda: row)) is row
    assert authority.executing[ref.session_id] == row['generation']
    assert published == [ref]
    assert live.control_claim['physical_session_id'] is None
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        _claim(authority, ref, live, row['admission_id'])


def test_captured_seed_excludes_preclaim_compression_ancestors(transcript):
    db, ref = transcript
    append_turn(db)
    db.create_session('claimed-tip', source='telegram', parent_session_id=ref.session_id)
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET end_reason='compression',ended_at=1 WHERE id=?", (ref.session_id,)))
    db.append_message('claimed-tip', 'user', 'actual admitted input', message_uid='new-user')
    db.append_message('claimed-tip', 'assistant', tool_calls=[{'id': 'new-call'}], message_uid='new-call-row')
    proof = read_control_proof(db, ref, 0, 'actual admitted input', 'claimed-tip')
    assert [row['message_uid'] for row in proof] == ['new-user', 'new-call-row']
    with pytest.raises(RuntimeStoreError, match='control_proof_unavailable'):
        read_control_proof(db, ref, 0, 'actual admitted input')


def test_optional_capture_stays_in_tracked_worker_without_blocking_owner_loop(transcript, monkeypatch):
    import threading
    from functools import partial
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_runtime_workers import mutation_tasks, tracked_write
    from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input

    db, ref = transcript
    epoch = begin_runtime_epoch(db, instance_id='responsive-owner')
    admit_session_input(db, epoch=epoch, principal_id='operator', session_id=ref.session_id,
                        request_id='responsive-request', payload={'text': 'actual admitted input'})
    authority = SessionAuthority(SimpleNamespace(), profile_id=ref.profile_id,
                                 instance_id='responsive-owner', db=db, epoch=epoch)
    live = authority.sessions[ref.session_id] = LiveSession(None, 'responsive-route')
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    def held_capture(store, target):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), 'test worker was not released'
        return target.session_id
    monkeypatch.setattr('gateway.session_control_viewer.capture_physical_target', held_capture)

    async def exercise():
        loop_thread = threading.get_ident()
        claim = partial(claim_session_input, db, epoch=epoch, session_id=ref.session_id)
        task = asyncio.create_task(tracked_write(authority,
            partial(authority._claim_with_control_target, ref, claim),
            then=partial(authority._stamp_claim, ref, live)))
        try:
            assert await asyncio.to_thread(entered.wait, 1), 'capture did not begin'
            tick = asyncio.Event()
            asyncio.get_running_loop().call_later(.01, tick.set)
            await asyncio.wait_for(tick.wait(), .5)
            assert worker_threads == [worker_threads[0]] and worker_threads[0] != loop_thread
            assert not task.done() and mutation_tasks(authority)
            assert ref.session_id not in authority.executing
        finally:
            release.set()
            row = await task
        assert authority.executing[ref.session_id] == row['generation']
        assert live.control_claim['physical_session_id'] == ref.session_id
        assert not mutation_tasks(authority)
    asyncio.run(exercise())
