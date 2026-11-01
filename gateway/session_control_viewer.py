"""Admission-bound control viewers: no agent mount, policy refresh, or transcript replay."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import asdict
from functools import partial
import hashlib
from itertools import pairwise
import json
from pathlib import Path
import sqlite3
import time
import uuid

from gateway.session_contract import CANONICAL_GATEWAY_PROTOCOL, SubscriptionSnapshot
from hermes_state_compression import _CHAIN_CAP
from hermes_state_runtime import RuntimeStoreError, get_session_admission

CONTROL_VIEWER_CAPABILITY = 'control-only-viewer-v1'
_PROOF_ROWS = 256
_PROOF_BYTES = 512 * 1024
_FIELD_BYTES = 128 * 1024
_LINEAGE_LIMIT = _CHAIN_CAP
_FANOUT_LIMIT = 64


def control_requested(params):
    return 'admission_id' in params or params.get('control_only', False) is not False


async def restore_before_dispatch(authority, ref, method, params):
    if method == 'session.resume' and control_requested(params):
        return
    if (isinstance(ref.session_id, str) and ref.session_id and not getattr(authority, 'retiring', False)
            and ref.profile_id == getattr(authority, 'profile_id', None)
            and ref.session_id not in getattr(authority, 'sessions', {ref.session_id: None})):
        from gateway.session_local_adoption import adopt_before_restore
        await adopt_before_restore(authority, ref.session_id)


async def resume_or_controls(connection, ref, params):
    if control_requested(params):
        return await resume_controls(connection, ref, params)
    return await connection.resume(ref, params)


def subscribe_viewer(authority, actor, ref, live):
    """Shared RAM subscription primitive; caller holds the live stream lock."""
    subscription = next((key for key, member in live.subscribers.items()
                         if member == actor), None) or uuid.uuid4().hex
    live.subscribers[subscription] = actor
    transport = authority.events.get(actor.transport_id)
    if transport is not None:
        live.event_stream.fanout.attach(transport)
        live.event_stream.on_overflow = partial(authority._retire_overflowed, ref.session_id)
    return subscription


def _unavailable():
    return RuntimeStoreError('control_proof_unavailable')


def _unique_continuations(conn, db, chain):
    for parent, expected in pairwise(chain):
        rows = conn.execute(
            'SELECT id,parent_session_id,source, CASE WHEN length(CAST(model_config AS BLOB))<=? '
            'THEN model_config END model_config, length(CAST(model_config AS BLOB)) config_bytes '
            'FROM sessions WHERE parent_session_id=? LIMIT ?',
            (_FIELD_BYTES, parent, _FANOUT_LIMIT + 1)).fetchall()
        if len(rows) > _FANOUT_LIMIT or any((row['config_bytes'] or 0) > _FIELD_BYTES for row in rows):
            raise _unavailable()
        eligible = [row['id'] for row in rows if row['source'] != 'tool'
                    and not db._is_explicit_fork_child_row(dict(row), include_reset=True)]
        if eligible != [expected]:
            raise _unavailable()


def _lineage(conn, db, ref):
    # Reuse the store's verified continuation selection on THIS read-only transaction.
    target = db._compression_tip_on(conn, ref.session_id)
    chain, seen = [], set()
    while target:
        if target in seen or len(chain) >= _LINEAGE_LIMIT:
            raise _unavailable()
        seen.add(target)
        row = conn.execute('SELECT id,parent_session_id,end_reason,source, '
                           'CASE WHEN length(CAST(model_config AS BLOB))<=? THEN model_config END model_config, '
                           'length(CAST(model_config AS BLOB)) config_bytes FROM sessions WHERE id=?',
                           (_FIELD_BYTES, target)).fetchone()
        if row is None or (row['config_bytes'] or 0) > _FIELD_BYTES:
            raise _unavailable()
        row = dict(row)
        chain.append(target)
        if db._is_explicit_fork_child_row(row, include_reset=True):
            break
        parent = row.get('parent_session_id')
        if not parent:
            break
        prior = conn.execute('SELECT end_reason FROM sessions WHERE id=?', (parent,)).fetchone()
        if prior is None or prior[0] != 'compression':
            break
        target = parent
    if ref.session_id not in chain:
        raise _unavailable()
    ordered = tuple(reversed(chain))
    _unique_continuations(conn, db, ordered)
    return ordered


def _read_rows(conn, chain, mark):
    placeholders = ','.join('?' for _ in chain)
    # Never materialize tool responses, images, reasoning, or assistant prose.
    sql = f'''SELECT id,message_uid,role,
        CASE WHEN role='user' AND length(CAST(content AS BLOB))<=? THEN content END content,
        CASE WHEN role='assistant' AND length(CAST(tool_calls AS BLOB))<=? THEN tool_calls END tool_calls,
        CASE WHEN role='user' THEN length(CAST(content AS BLOB))
             ELSE length(CAST(tool_calls AS BLOB)) END field_bytes
        FROM messages WHERE session_id IN ({placeholders}) AND id>?
        AND (role='user' OR (role='assistant' AND tool_calls IS NOT NULL))
        ORDER BY id LIMIT ?'''
    rows, used = [], 0
    for row in conn.execute(sql, (_FIELD_BYTES, _FIELD_BYTES, *chain, mark, _PROOF_ROWS + 1)):
        if (len(rows) >= _PROOF_ROWS or not isinstance(row['message_uid'], str)
                or not row['message_uid'] or (row['field_bytes'] or 0) > _FIELD_BYTES):
            raise _unavailable()
        used += row['field_bytes'] or 0
        if used > _PROOF_BYTES:
            raise _unavailable()
        rows.append(dict(row))
    return rows


def _proof_message(row):
    item = {'role': row['role'], 'message_uid': row['message_uid']}
    if row['role'] == 'user':
        if not isinstance(row['content'], str):
            raise _unavailable()
        item['content'] = row['content']
    else:
        try:
            calls = json.loads(row['tool_calls'])
        except (TypeError, ValueError) as exc:
            raise _unavailable() from exc
        if not isinstance(calls, list) or not calls:
            raise _unavailable()
        item['tool_calls'] = calls
    return item


def _project_rows(conn, chain, mark, rows, admitted_text):
    if not rows:
        raise _unavailable()
    ids = list({row['message_uid'] for row in rows})
    # Compression can copy pre-turn context above the claim mark. Stable identity, not
    # the new physical row id, keeps that old context outside this turn's proof.
    marks, scope = ','.join('?' for _ in ids), ','.join('?' for _ in chain)
    prior = {row[0] for row in conn.execute(
        f'SELECT DISTINCT message_uid FROM messages WHERE session_id IN ({scope}) '
        f'AND id<=? AND message_uid IN ({marks})', (*chain, mark, *ids))}
    messages, seen = [], {}
    for row in rows:
        uid = row['message_uid']
        if uid in prior:
            continue
        item = _proof_message(row)
        if uid in seen:
            if seen[uid] != item:
                raise _unavailable()
            continue
        seen[uid] = item
        messages.append(item)
    anchors = [index for index, row in enumerate(messages)
               if row['role'] == 'user' and row['content'] == admitted_text]
    # A pre-existing/open user tail is NOT synthesized from the admission payload.
    # Without its executed post-claim identity this mode cannot prove its call.
    if len(anchors) != 1 or anchors[0] != 0 or len(messages) < 2:
        raise _unavailable()
    return tuple(messages)


def read_control_proof(db, ref, mark, admitted_text):
    """A separate read-only SQLite snapshot with hard row/byte/lineage/time bounds."""
    if type(mark) is not int or mark < 0 or not isinstance(admitted_text, str) or not admitted_text:
        raise _unavailable()
    deadline = time.monotonic() + 1
    path = Path(db.db_path).absolute().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(path, uri=True, timeout=.2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            try:
                conn.execute('BEGIN')
                chain = _lineage(conn, db, ref)
                rows = _read_rows(conn, chain, mark)
                proof = _project_rows(conn, chain, mark, rows, admitted_text)
                conn.execute('ROLLBACK')
                if _lineage(conn, db, ref) != chain:
                    raise _unavailable()
            finally:
                conn.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        raise _unavailable() from exc
    return proof


def _claim(authority, ref, live, admission_id):
    row = get_session_admission(authority.db, admission_id=admission_id)
    stamp = live.control_claim
    if (not row or row['target_session_id'] != ref.session_id
            or row['status'] != 'started' or row['owner_epoch'] != authority.epoch
            or not isinstance(stamp, dict) or stamp.get('admission_id') != admission_id
            or stamp.get('generation') != row['generation']
            or authority.executing.get(ref.session_id) != row['generation']):
        raise _unavailable()
    return row, dict(stamp)


async def attach_controls(authority, actor, ref, admission_id):
    # authorize() may cold-adopt a missing local session: this observer must not.
    if ref.session_id not in authority.sessions:
        raise RuntimeStoreError('not_found')
    authority.authorize(actor, ref, 'session:read')
    live = authority.sessions[ref.session_id]
    with live.event_stream.lock:
        row, stamp = _claim(authority, ref, live, admission_id)
        generation = row['generation']
        prompts = live.controls.snapshot(ref.session_id, generation)
        admitted_text = authority._pending_receipt(row).text
    history = ()
    if prompts:
        history = await asyncio.to_thread(read_control_proof, authority.db, ref,
                                          stamp.get('transcript_mark'), admitted_text)
    if authority.sessions.get(ref.session_id) is not live:
        raise _unavailable()
    authority.authorize(actor, ref, 'session:read')
    with live.event_stream.lock:
        current, fresh_stamp = _claim(authority, ref, live, admission_id)
        if (authority.sessions.get(ref.session_id) is not live or fresh_stamp != stamp
                or live.controls.snapshot(ref.session_id, generation) != prompts):
            raise _unavailable()
        subscription = subscribe_viewer(authority, actor, ref, live)
        epoch, sequence = live.event_stream.watermark()
        return SubscriptionSnapshot(subscription, authority._handle(ref), epoch, sequence,
            history, (authority._pending_receipt(current),), prompts)


async def resume_controls(connection, ref, params):
    allowed = {'session_id', 'profile', 'control_only', 'admission_id'}
    admission_id = params.get('admission_id')
    if (set(params) - allowed or params.get('control_only') is not True
            or not isinstance(admission_id, str) or not admission_id or len(admission_id) > 128):
        raise RuntimeStoreError('invalid_params')
    authority = connection.authority
    snapshot = await authority.attach(connection.actor, ref, control_only=True, admission_id=admission_id)
    connection.subscriptions[ref.session_id] = snapshot.subscription_id
    text = snapshot.pending[0].text
    return {'session_id': ref.session_id, 'stored_session_id': ref.session_id,
        'messages': list(snapshot.history), 'message_count': len(snapshot.history),
        'running': snapshot.handle.execution_state == 'running',
        'authority_epoch': snapshot.handle.authority_epoch,
        'replay_epoch': snapshot.replay_epoch, 'last_sequence': snapshot.last_sequence,
        'subscription_id': snapshot.subscription_id, 'revision': snapshot.handle.revision,
        'execution_generation': snapshot.handle.execution_generation,
        'pending': [asdict(row) for row in snapshot.pending], 'prompts': list(snapshot.prompts),
        'info': {'desktop_protocol': CANONICAL_GATEWAY_PROTOCOL}, 'control_only': True,
        'control_proof': {'version': 1, 'admission_id': admission_id,
            'profile_id': ref.profile_id, 'session_id': ref.session_id,
            'authority_epoch': snapshot.handle.authority_epoch,
            'execution_generation': snapshot.handle.execution_generation,
            'admitted_input_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}}
