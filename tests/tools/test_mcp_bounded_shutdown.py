"""Regression coverage for bounded one-shot MCP process teardown."""

import sys
import threading
from types import SimpleNamespace


def test_empty_registry_shutdown_reaps_tracked_active_children(monkeypatch):
    from tools import mcp_tool

    calls = []
    monkeypatch.setattr(mcp_tool, "_stop_mcp_loop", lambda: None)
    monkeypatch.setattr(
        mcp_tool,
        "_kill_orphaned_mcp_children",
        lambda **kwargs: calls.append(kwargs),
    )
    with mcp_tool._lock:
        mcp_tool._servers.clear()

    mcp_tool.shutdown_mcp_servers()

    assert calls == [{"include_active": True}]


def test_bounded_shutdown_forces_sweep_after_timeout(monkeypatch):
    from tools import mcp_tool

    release = threading.Event()
    calls = []

    def _wedged_shutdown():
        release.wait(5)

    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", _wedged_shutdown)
    monkeypatch.setattr(
        mcp_tool,
        "_kill_orphaned_mcp_children",
        lambda **kwargs: calls.append(kwargs),
    )
    try:
        assert mcp_tool.shutdown_mcp_servers_bounded(timeout=0.01) is False
    finally:
        release.set()

    assert calls == [{
        "include_active": True,
        "tracking_lock_timeout": 0.25,
    }]


def test_windows_family_shutdown_terminates_descendants_before_root(monkeypatch):
    from tools import mcp_tool

    events = []

    class _Process:
        def __init__(self, pid, children=()):
            self.pid = pid
            self._children = list(children)

        def children(self, recursive=False):
            assert recursive is True
            return self._children

        def terminate(self):
            events.append(("terminate", self.pid))

        def kill(self):
            events.append(("kill", self.pid))

    grandchild = _Process(103)
    child = _Process(102, [grandchild])
    root = _Process(101, [child, grandchild])
    processes = {101: root}

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    fake_psutil = SimpleNamespace(
        Process=lambda pid: processes[pid],
        NoSuchProcess=_NoSuchProcess,
        AccessDenied=_AccessDenied,
        wait_procs=lambda family, timeout: (family[:-1], family[-1:]),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    mcp_tool._terminate_windows_mcp_families(
        {101: "fixture"},
        grace_seconds=0,
    )

    assert events == [
        ("terminate", 103),
        ("terminate", 102),
        ("terminate", 101),
        ("kill", 101),
    ]
