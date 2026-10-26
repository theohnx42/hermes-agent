"""The preview is an explicit REST read option; default/full history remains lossless."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    import hermes_cli._early_recovery
    from hermes_state import SessionDB
    monkeypatch.setattr(hermes_cli._early_recovery, 'restore_interrupted_pull', lambda root: False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr('hermes_state.DEFAULT_DB_PATH', home / 'state.db')
    db = SessionDB(home / 'state.db')
    db.create_session('large', 'desktop')
    db.append_message('large', 'user', 'Z' * 4096)
    db.close()
    from hermes_cli.web_routers.sessions import manage_router
    app = FastAPI()
    app.include_router(manage_router)
    with TestClient(app) as client:
        yield client


def test_explicit_preview_honestly_reports_size_and_full_read_stays_available(client):
    path = '/api/sessions/large/messages?limit=120&order=latest&include_compacted=true'
    preview = client.get(path + '&preview_chars=256')
    assert preview.status_code == 200
    message = preview.json()['messages'][0]
    assert message['content_preview']['original_chars'] == 4096
    assert message['content_preview']['truncated'] is True
    assert message['content_preview']['full_content_available'] is True
    assert len(message['content']) < 600
    full = client.get(path)
    assert full.status_code == 200
    assert full.json()['messages'][0]['content'] == 'Z' * 4096
    assert 'content_preview' not in full.json()['messages'][0]


@pytest.mark.parametrize('value', ['0', '255', '65537', 'not-an-int'])
def test_invalid_preview_limit_is_rejected(client, value):
    assert client.get('/api/sessions/large/messages?preview_chars=' + value).status_code == 422
