"""End-to-end one-shot exit and MCP process-family lifecycle regression."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


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
