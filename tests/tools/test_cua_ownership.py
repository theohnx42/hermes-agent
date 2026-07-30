"""Cross-process CUA ownership and handoff contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tools.computer_use.ownership import CuaOwnershipBusy, CuaOwnershipLease


def _probe(root: Path, role: str, wait: float = 0.0) -> subprocess.CompletedProcess:
    program = """
import sys
from pathlib import Path
from tools.computer_use.ownership import CuaOwnershipLease
lease = CuaOwnershipLease(Path(sys.argv[1]), sys.argv[2], wait_seconds=float(sys.argv[3]))
try:
    lease.acquire()
except Exception as exc:
    print(type(exc).__name__ + ":" + str(exc))
    raise SystemExit(2)
print("acquired")
lease.release()
"""
    return subprocess.run(
        [sys.executable, "-c", program, str(root), role, str(wait)],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_desktop_cannot_duplicate_gateway_worker_then_acquires_on_handoff(
    tmp_path,
):
    root = tmp_path / "cua"
    gateway = CuaOwnershipLease(root, "gateway")
    gateway.acquire()
    try:
        blocked = _probe(root, "desktop", wait=0.1)
        assert blocked.returncode == 2
        assert "CuaOwnershipBusy" in blocked.stdout
        assert "owned by gateway" in blocked.stdout
    finally:
        gateway.release()

    acquired = _probe(root, "desktop", wait=0.5)
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout == "acquired\n"


def test_explicit_desktop_request_cooperatively_yields_gateway(tmp_path):
    root = tmp_path / "cua"
    yielded = threading.Event()
    gateway = None

    def handoff():
        gateway.release()
        yielded.set()

    gateway = CuaOwnershipLease(
        root,
        "gateway",
        handoff_callback=handoff,
    )
    gateway.acquire()

    desktop = _probe(root, "desktop", wait=2.0)

    assert desktop.returncode == 0, desktop.stdout + desktop.stderr
    assert yielded.wait(1.0)


def test_crashed_owner_is_recovered_by_kernel_lock(tmp_path):
    root = tmp_path / "cua"
    ready = tmp_path / "ready"
    program = """
import os, sys
from pathlib import Path
from tools.computer_use.ownership import CuaOwnershipLease
lease = CuaOwnershipLease(Path(sys.argv[1]), "gateway")
lease.acquire()
Path(sys.argv[2]).write_text("ready")
os._exit(23)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", program, str(root), str(ready)],
        cwd=Path(__file__).resolve().parents[2],
        timeout=10,
        check=False,
    )
    assert crashed.returncode == 23
    assert ready.read_text() == "ready"

    desktop = CuaOwnershipLease(root, "desktop", wait_seconds=0.5)
    desktop.acquire()
    desktop.release()


def test_diagnostic_metadata_never_authorizes_or_kills_unrelated_driver(
    tmp_path,
):
    root = tmp_path / "cua"
    gateway = CuaOwnershipLease(root, "gateway")
    gateway.acquire()
    unrelated = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "cua-driver",
            "serve",
        ]
    )
    owner = json.loads((root / "owner.json").read_text())
    owner["pid"] = unrelated.pid
    owner["command"] = "cua-driver serve --someone-elses-socket"
    (root / "owner.json").write_text(json.dumps(owner))
    try:
        blocked = _probe(root, "desktop", wait=0.01)
        assert blocked.returncode == 2
        assert unrelated.poll() is None
    finally:
        gateway.release()
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_same_process_backends_share_refcounted_lease(tmp_path):
    root = tmp_path / "cua"
    first = CuaOwnershipLease(root, "gateway")
    second = CuaOwnershipLease(root, "gateway")
    first.acquire()
    second.acquire()
    first.release()
    assert _probe(root, "desktop").returncode == 2
    second.release()
    assert _probe(root, "desktop").returncode == 0


def test_backend_start_failure_releases_machine_lease(monkeypatch, tmp_path):
    from tools import lazy_deps
    from tools.computer_use import cua_backend

    root = tmp_path / "cua"
    backend = cua_backend.CuaDriverBackend()
    backend.configure_ownership("gateway", root=root)
    monkeypatch.setattr(cua_backend, "_maybe_nudge_update", lambda: None)
    monkeypatch.setattr(lazy_deps, "ensure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        backend._session,
        "start",
        lambda: (_ for _ in ()).throw(RuntimeError("MCP start failed")),
    )

    with pytest.raises(RuntimeError, match="MCP start failed"):
        backend.start()

    assert _probe(root, "desktop").returncode == 0


def test_livebase_global_backend_is_bound_to_surface_ownership(monkeypatch):
    """The current single-backend architecture still enters the shared lease."""
    from tools.computer_use import cua_backend, tool

    calls = []

    class _FakeBackend:
        def configure_ownership(self, role, *, handoff_callback=None, **_kwargs):
            calls.append(("configure", role, handoff_callback))

        def start(self):
            calls.append(("start",))

        def stop(self):
            calls.append(("stop",))

    monkeypatch.setattr(cua_backend, "CuaDriverBackend", _FakeBackend)
    monkeypatch.setenv("HERMES_CUA_OWNER_ROLE", "gateway")
    tool._backend = None

    backend = tool._get_backend()

    assert isinstance(backend, _FakeBackend)
    assert calls[0][0:2] == ("configure", "gateway")
    assert callable(calls[0][2])
    assert calls[1] == ("start",)

    calls[0][2]()
    assert tool._backend is None
    assert calls[-1] == ("stop",)
