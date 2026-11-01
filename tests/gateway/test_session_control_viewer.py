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
    authority._stamp_claim(ref, live, row)
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
