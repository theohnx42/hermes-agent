"""REST display reads follow existing local receipts without changing logical ownership."""
import json
from pathlib import Path

import pytest
from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, local_transcript_target
from hermes_state_runtime import RuntimeStoreError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from aiohttp import web
from aiohttp.test_utils import TestClient as AsyncClient, TestServer
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def store(tmp_path, monkeypatch):
    import hermes_cli._early_recovery
    monkeypatch.setattr(hermes_cli._early_recovery, 'restore_interrupted_pull', lambda root: False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    db = SessionDB(home / 'state.db')
    for sid in ('logical', 'current'):
        db.create_session(sid, 'desktop', session_key='local:logical', chat_id='logical', user_id='owner',
                          **({'parent_session_id': 'logical'} if sid == 'current' else {}))
    db.append_message('logical', 'user', 'OLD_RESET_HISTORY')
    db.end_session('logical', 'session_reset')
    current_row = db.append_message('current', 'user', 'CURRENT_HISTORY')
    receipt = {'profile_id': str(home.resolve()), 'session_id': 'logical', 'principal_id': 'owner',
               'route': 'local:logical', 'lineage': ['logical', 'current'],
               'entry': {'session_id': 'current', 'session_key': 'local:logical',
                         'origin': {'platform': 'local', 'chat_id': 'logical', 'user_id': 'owner'}}}
    db.set_meta(POLICY_PREFIX + 'logical', json.dumps(receipt))
    yield db, receipt, current_row
    db.close()


def test_readonly_target_uses_receipt_not_reset_parent_and_never_writes(store, monkeypatch):
    writer, _, _ = store
    with SessionDB(writer.db_path, read_only=True) as reader:
        def forbidden(*args, **kwargs):
            raise AssertionError('Read-only transcript resolution attempted a write')
        monkeypatch.setattr(reader, '_execute_write', forbidden)
        assert local_transcript_target(reader, 'logical') == 'current'
        assert reader.get_session('logical')['end_reason'] == 'session_reset'
        assert reader.get_messages('logical')[0]['content'] == 'OLD_RESET_HISTORY'


def test_no_receipt_keeps_existing_legacy_compression_resolution(store):
    db, _, _ = store
    db.create_session('legacy', 'cli')
    db.end_session('legacy', 'compression')
    db.create_session('legacy-tip', 'cli', parent_session_id='legacy')
    assert local_transcript_target(db, 'legacy') is None
    assert db.resolve_resume_session_id('legacy') == 'legacy-tip'


@pytest.mark.parametrize('change', ['corrupt', 'missing-child', 'foreign-home', 'foreign-row',
                                  'wrong-mode', 'wrong-route', 'wrong-lineage', 'wrong-owner'])
def test_invalid_present_receipt_fails_closed(store, change):
    db, receipt, _ = store
    if change == 'corrupt':
        db.set_meta(POLICY_PREFIX + 'logical', '{PRIVATE_CORRUPT')
    else:
        if change == 'missing-child':
            receipt['entry']['session_id'] = 'missing'
            receipt['lineage'].append('missing')
        elif change == 'foreign-home':
            receipt['profile_id'] = str(Path(db.db_path).parent / 'foreign')
        elif change == 'foreign-row':
            db._write_sql('UPDATE sessions SET profile_name=? WHERE id=?', ('other', 'current'))
        elif change == 'wrong-mode':
            receipt['entry']['origin']['platform'] = 'telegram'
        elif change == 'wrong-route':
            receipt['entry']['session_key'] = 'other-route'
        elif change == 'wrong-lineage':
            receipt['lineage'] = ['logical']
        elif change == 'wrong-owner':
            receipt['entry']['origin']['user_id'] = 'other-owner'
        db.set_meta(POLICY_PREFIX + 'logical', json.dumps(receipt))
    with pytest.raises(RuntimeStoreError) as error:
        local_transcript_target(db, 'logical')
    assert str(error.value) == 'storage_unavailable'


@pytest.fixture
def client(store):
    from hermes_cli.web_routers.sessions import manage_router
    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        yield client


def test_fastapi_messages_timeline_and_around_preserve_logical_id(client, store):
    _, _, row_id = store
    for path in ('/messages?limit=120&order=latest&include_compacted=true', '/timeline?limit=500',
                 f'/messages/around?row_id={row_id}&limit=120'):
        response = client.get('/api/sessions/logical' + path)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['session_id'] == 'logical'
        assert data['transcript_session_id'] == 'current'
        assert 'OLD_RESET_HISTORY' not in response.text
        if 'messages' in data:
            assert data['messages'][0]['content'] == 'CURRENT_HISTORY'


def test_fastapi_corrupt_receipt_is_503_not_old_transcript(client, store):
    db, _, _ = store
    db.set_meta(POLICY_PREFIX + 'logical', '{PRIVATE_CORRUPT')
    response = client.get('/api/sessions/logical/messages')
    assert response.status_code == 503
    assert 'OLD_RESET_HISTORY' not in response.text
    assert 'PRIVATE_CORRUPT' not in response.text


@pytest.mark.asyncio
async def test_native_adapter_follows_current_target_and_preserves_scope(store):
    db, receipt, _ = store
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._session_db = db
    app = web.Application()
    app.router.add_get('/api/sessions/{session_id}/messages', adapter._handle_session_messages)
    async with AsyncClient(TestServer(app)) as client:
        response = await client.get('/api/sessions/logical/messages?limit=120&include_compacted=true')
        assert response.status == 200
        data = await response.json()
        assert data['session_id'] == 'logical'
        assert data['transcript_session_id'] == 'current'
        assert [row['content'] for row in data['data']] == ['CURRENT_HISTORY']
        receipt['profile_id'] = str(Path(db.db_path).parent / 'foreign')
        db.set_meta(POLICY_PREFIX + 'logical', json.dumps(receipt))
        refused = await client.get('/api/sessions/logical/messages')
        assert refused.status == 503
        assert 'CURRENT_HISTORY' not in await refused.text()
