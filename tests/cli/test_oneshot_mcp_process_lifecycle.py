"""End-to-end one-shot exit and MCP process-family lifecycle regression."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from hermes_state import SessionDB


CHILD_PROGRAM = r"""
import os
from pathlib import Path
import subprocess
import sys
import time

scenario, workdir = sys.argv[1:3]
workdir = Path(workdir)
grandchild_file = workdir / "grandchild.pid"
helper_code = (
    "from pathlib import Path; import subprocess, sys, time; "
    "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
)
spawn_kwargs = {}
if os.name == "nt":
    spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    spawn_kwargs["start_new_session"] = True
helper = subprocess.Popen(
    [sys.executable, "-c", helper_code, str(grandchild_file)],
    **spawn_kwargs,
)
(workdir / "root.pid").write_text(str(helper.pid))
deadline = time.monotonic() + 3
while not grandchild_file.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not grandchild_file.exists():
    raise RuntimeError("helper did not publish grandchild PID")

from tools import mcp_tool
with mcp_tool._lock:
    mcp_tool._stdio_pids[helper.pid] = "oneshot-regression"
    if os.name != "nt":
        mcp_tool._stdio_pgids[helper.pid] = os.getpgid(helper.pid)

if scenario == "cleanup-timeout":
    def wedged_shutdown():
        time.sleep(60)
    mcp_tool.shutdown_mcp_servers = wedged_shutdown

import hermes_cli.oneshot
if scenario == "exception":
    def fake_run_oneshot(_prompt, **_kwargs):
        raise RuntimeError("deterministic one-shot failure")
else:
    def fake_run_oneshot(_prompt, **_kwargs):
        print("ONESHOT-EXACT")
        return 0
hermes_cli.oneshot.run_oneshot = fake_run_oneshot

from hermes_cli.main import _run_and_exit_oneshot
_run_and_exit_oneshot("ignored")
"""


def _pid_exists(pid: int) -> bool:
    try:
        import psutil

        process = psutil.Process(pid)
        return process.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except psutil.NoSuchProcess:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_family_baseline(pids, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if not any(_pid_exists(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return not any(_pid_exists(pid) for pid in pids)


def _force_cleanup(pid: int) -> None:
    if not _pid_exists(pid):
        return
    try:
        if os.name == "nt":
            import psutil

            root = psutil.Process(pid)
            family = root.children(recursive=True) + [root]
            for process in reversed(family):
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@pytest.mark.parametrize(
    ("scenario", "expected_code", "expected_stdout"),
    [
        ("success", 0, "ONESHOT-EXACT\n"),
        ("cleanup-timeout", 0, "ONESHOT-EXACT\n"),
        ("exception", 1, ""),
    ],
)
def test_oneshot_exits_and_restores_mcp_family_baseline_within_ten_seconds(
    tmp_path,
    scenario,
    expected_code,
    expected_stdout,
):
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", CHILD_PROGRAM, scenario, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "HERMES_HOME": str(tmp_path / "hermes-home")},
    )
    root_pid = int((tmp_path / "root.pid").read_text())
    grandchild_pid = int((tmp_path / "grandchild.pid").read_text())
    try:
        restored = _wait_for_family_baseline(
            (root_pid, grandchild_pid),
            deadline=started + 10,
        )
        elapsed = time.monotonic() - started
        assert result.returncode == expected_code, result.stderr
        assert result.stdout == expected_stdout
        assert elapsed < 10
        assert restored
        if scenario == "exception":
            assert "deterministic one-shot failure" in result.stderr
    finally:
        _force_cleanup(root_pid)
        _force_cleanup(grandchild_pid)


def test_oneshot_process_cleanup_does_not_end_independent_rotation_tip(
    monkeypatch, tmp_path
):
    """Process-global teardown owns MCP children, never durable chat lifecycle."""
    parent = "independent-parent"
    child = "independent-rotation-tip"
    goal = {"goal": "survive unrelated one-shot teardown", "status": "active"}
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.create_session(parent, source="webui", cwd="/work/project")
    db.set_session_title(parent, "Independent rotated session")
    db.set_meta(f"goal:{parent}", json.dumps(goal))
    assert db.try_acquire_compression_lock(parent, "winner", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id=child,
        source="webui",
        messages=[{"role": "user", "content": "continue independently"}],
        compression_lock_holder="winner",
        local_rotation=True,
        lineage_compression_count=3,
    )
    db.close()

    from agent import auxiliary_client
    from hermes_cli import main
    from tools import async_delegation, browser_tool, terminal_tool

    monkeypatch.setattr(terminal_tool, "cleanup_all_environments", lambda: None)
    monkeypatch.setattr(async_delegation, "interrupt_all", lambda **_kwargs: None)
    monkeypatch.setattr(browser_tool, "_emergency_cleanup_all_sessions", lambda: None)
    monkeypatch.setattr(auxiliary_client, "shutdown_cached_clients", lambda: None)
    monkeypatch.setattr(main, "_oneshot_cleanup_done", False)

    main._cleanup_oneshot_runtime()

    reopened = SessionDB(db_path=db_path)
    try:
        tip = reopened.get_session(child)
        assert reopened.resolve_resume_session_id(parent) == child
        assert tip["ended_at"] is None
        assert tip["end_reason"] is None
        assert tip["title"] == "Independent rotated session"
        assert json.loads(reopened.get_meta(f"goal:{child}")) == goal
        assert reopened.get_messages_as_conversation(child)[-1]["content"] == (
            "continue independently"
        )
    finally:
        reopened.close()
