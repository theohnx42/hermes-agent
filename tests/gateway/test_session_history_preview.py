"""The actual native API adapter honors explicit previews without changing full reads."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from hermes_state import SessionDB
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_adapter_preview_metadata_default_full_read_and_invalid_budget(tmp_path, monkeypatch):
    import hermes_cli._early_recovery
    monkeypatch.setattr(hermes_cli._early_recovery, 'restore_interrupted_pull', lambda root: False)
    import inspect
    assert hasattr(SessionDB, 'get_message_previews'), (inspect.getfile(SessionDB), inspect.getfile(SessionDB.__mro__[2]))
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('large', 'desktop')
        db.append_message('large', 'user', 'A' * 4096)
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter._session_db = db
        app = web.Application()
        app.router.add_get('/api/sessions/{session_id}/messages', adapter._handle_session_messages)
        async with TestClient(TestServer(app)) as client:
            path = '/api/sessions/large/messages?limit=120&order=latest&include_compacted=true'
            response = await client.get(path + '&preview_chars=256')
            assert response.status == 200
            message = (await response.json())['data'][0]
            assert message['content_preview'] == {'truncated': True, 'original_chars': 4096,
                                                  'shown_chars': 256, 'full_content_available': True}
            assert len(message['content']) < 600
            full = await client.get(path)
            assert full.status == 200
            row = (await full.json())['data'][0]
            assert row['content'] == 'A' * 4096
            assert 'content_preview' not in row
            for value in ('0', '255', '65537', 'not-an-int'):
                invalid = await client.get(path + '&preview_chars=' + value)
                assert invalid.status == 400
    finally:
        db.close()
