"""Explicit owner reload reuses the existing changed-input MCP reconnect path."""
import asyncio
from contextlib import nullcontext
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import session_ancillary


def _connection():
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.executescript('CREATE TABLE session_admissions(status TEXT); CREATE TABLE worker_executions(status TEXT);')
    authority = SimpleNamespace(db=SimpleNamespace(_read_ctx=lambda: nullcontext(db)), profile_id='fixture',
        runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=False),
                               _mcp_reload_refresh_cached_agents=lambda *args: None))
    return SimpleNamespace(authority=authority, actor=SimpleNamespace(capabilities={'session:control'})), db


def _discovery(monkeypatch, changed):
    monkeypatch.setattr('gateway.session_authorities.served_profile_name', lambda profile: 'default')
    monkeypatch.setattr('tools.mcp_tool_discovery.reconcile_mcp_servers_with_config',
                        lambda: {'removed': [], 'added': [], 'pending': []})
    monkeypatch.setattr('tools.mcp_tool_discovery.mcp_servers_ready', lambda: ['intel-search', 'keep'])
    monkeypatch.setattr('tools.mcp_tool_discovery.mcp_servers_not_connected', list)
    restart = Mock(return_value=changed)
    monkeypatch.setattr('tools.mcp_tool_discovery.restart_mcp_servers_with_new_inputs', restart)
    return restart


def test_idle_explicit_reload_reconnects_changed_inputs_under_same_server_name(monkeypatch):
    connection, db = _connection()
    restart = _discovery(monkeypatch, ['intel-search'])
    try:
        result = asyncio.run(session_ancillary.reload_mcp(connection, None, {'confirm': True}))
        restart.assert_called_once_with({'intel-search', 'keep'})
        assert result['status'] == 'reloaded'
        assert 'restarted: intel-search' in result['message']
    finally:
        db.close()


@pytest.mark.parametrize(('table', 'state'), [
    ('session_admissions', 'queued'), ('session_admissions', 'started'), ('session_admissions', 'unknown'),
    ('worker_executions', 'registered'), ('worker_executions', 'running'), ('worker_executions', 'unknown'),
])
def test_changed_input_reconnect_leaves_all_accepted_profile_work_alone(monkeypatch, table, state):
    connection, db = _connection()
    restart = _discovery(monkeypatch, ['intel-search'])
    try:
        db.execute(f'INSERT INTO {table}(status) VALUES(?)', (state,))
        result = asyncio.run(session_ancillary.reload_mcp(connection, None, {'confirm': True}))
        restart.assert_not_called()
        assert 'restarted' not in result['message']
    finally:
        db.close()


def test_unchanged_inputs_keep_live_connections(monkeypatch):
    connection, db = _connection()
    restart = _discovery(monkeypatch, [])
    try:
        result = asyncio.run(session_ancillary.reload_mcp(connection, None, {'confirm': True}))
        restart.assert_called_once()
        assert 'no server changes' in result['message']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admission_ordering_fences_reconnect_against_new_work(monkeypatch):
    connection, db = _connection()
    restart = _discovery(monkeypatch, ['intel-search'])
    order = connection.authority._ordered_writes = asyncio.Lock()
    await order.acquire()
    task = asyncio.create_task(session_ancillary.reload_mcp(connection, None, {'confirm': True}))
    try:
        await asyncio.sleep(0)
        assert not task.done()
        # An admission already owning the ordering gate commits before reload.
        db.execute("INSERT INTO session_admissions(status) VALUES('queued')")
        order.release()
        await task
        restart.assert_not_called()
    finally:
        if order.locked():
            order.release()
        db.close()
