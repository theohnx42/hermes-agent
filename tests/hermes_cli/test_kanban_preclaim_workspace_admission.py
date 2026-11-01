"""Pre-claim workspace rejection against this maintained source tree, with isolated state."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def proposal_modules(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_workspace as kbw

    monkeypatch.setattr(kbd, "_kbw", kbw)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)

    home = tmp_path / ".hermes"
    home.mkdir()
    for name in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path().resolve()))
    kb.init_db()
    yield kb, kbc, kbd, kbw


def _events(kb, conn, task_id):
    return [(event.kind, event.payload, event.run_id) for event in kb.list_events(conn, task_id)]


@pytest.mark.parametrize(
    ("path", "style"),
    [
        ("", "empty"),
        ("repo", "relative"),
        ("/home/austin/repo", "posix_absolute"),
        ("C:\\repo", "windows_drive"),
        ("C:/repo", "windows_drive"),
        ("\\\\server\\share", "windows_unc"),
        ("//server/share", "windows_unc"),
    ],
)
def test_workspace_path_classifier_table(proposal_modules, path, style):
    _kb, _kbc, _kbd, kbw = proposal_modules
    assert kbw.workspace_path_style(path) == style


@pytest.mark.parametrize(
    ("host_platform", "path", "reason"),
    [
        ("win32", "/home/austin/repo", "posix_absolute_path_on_windows"),
        ("linux", "C:\\repo", "windows_absolute_path_on_non_windows"),
        ("linux", "C:/repo", "windows_absolute_path_on_non_windows"),
        ("linux", "\\\\server\\share", "windows_absolute_path_on_non_windows"),
        ("linux", "//server/share", "windows_absolute_path_on_non_windows"),
    ],
)
def test_incompatible_path_is_rejected_before_claim_with_no_run_or_spawn(
    proposal_modules, monkeypatch, host_platform, path, reason,
):
    kb, kbc, kbd, kbw = proposal_modules
    monkeypatch.setattr(kbw.sys, "platform", host_platform)
    spawned = []
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="wrong host path", assignee="default",
                                 workspace_kind="dir", workspace_path=path)
        result = kbd.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args))
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)
        events = _events(kb, conn, task_id)
    assert result.dispatch_rejected == [task_id]
    assert spawned == []
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert runs == []
    rejected = [event for event in events if event[0] == "dispatch_rejected"]
    assert rejected == [("dispatch_rejected", {
        "reason": reason, "host_platform": host_platform,
        "workspace_kind": "dir", "workspace_path": path,
        "source_status": "ready", "run_id": None,
    }, None)]


def test_compatible_path_preserves_claim_and_spawn(proposal_modules, tmp_path):
    kb, kbc, kbd, _kbw = proposal_modules
    workspace = tmp_path / "workspace"
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="compatible", assignee="default",
                                 workspace_kind="dir", workspace_path=str(workspace))
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_args: 4321)
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)
    assert [entry[0] for entry in result.spawned] == [task_id]
    assert result.dispatch_rejected == []
    assert task.status == "running"
    assert len(runs) == 1


def test_cas_loser_does_not_add_duplicate_rejection_event(proposal_modules, monkeypatch):
    kb, kbc, kbd, kbw = proposal_modules
    monkeypatch.setattr(kbw.sys, "platform", "win32")
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="raced", assignee="default",
                                 workspace_kind="dir", workspace_path="/repo")
        # Inject the interleaving directly at the CAS helper boundary. A
        # prelocked row would be filtered by _lane_rows and would not exercise
        # the conditional admission UPDATE at all.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_lock = 'other' WHERE id = ?", (task_id,))
        rejected = kbd._reject_preclaim_workspace_mismatch(
            conn, task_id, reason="posix_absolute_path_on_windows", lane="ready",
            workspace_kind="dir", workspace_path="/repo",
        )
        task = kb.get_task(conn, task_id)
        events = _events(kb, conn, task_id)
        runs = kb.list_runs(conn, task_id)
    assert rejected is False
    assert task.status == "ready"
    assert [event for event in events if event[0] == "dispatch_rejected"] == []
    assert runs == []


def test_review_lane_rejects_before_claim_with_no_run_or_spawn(proposal_modules, monkeypatch):
    kb, kbc, kbd, kbw = proposal_modules
    monkeypatch.setattr(kbw.sys, "platform", "win32")
    spawned = []
    with kbc.connect() as conn:
        task_id = kb.create_task(title="review wrong host path", assignee="default",
                                 workspace_kind="dir", workspace_path="/repo", conn=conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
        result = kbd.dispatch_once(conn, spawn_fn=lambda *args: spawned.append(args))
        task = kb.get_task(conn, task_id)
        runs = kb.list_runs(conn, task_id)
        events = _events(kb, conn, task_id)
    assert result.dispatch_rejected == [task_id]
    assert spawned == []
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert runs == []
    assert [event for event in events if event[0] == "dispatch_rejected"] == [(
        "dispatch_rejected", {
            "reason": "posix_absolute_path_on_windows", "host_platform": "win32",
            "workspace_kind": "dir", "workspace_path": "/repo",
            "source_status": "review", "run_id": None,
        }, None,
    )]
