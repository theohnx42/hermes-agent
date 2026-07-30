"""Cross-process ownership lease for Hermes-managed CUA workers.

The open kernel lock is authoritative. Metadata is diagnostic only: Hermes
never discovers, signals, or kills an unrelated ``cua-driver serve`` process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from typing import BinaryIO, Callable, Dict, Optional


class CuaOwnershipBusy(RuntimeError):
    pass


_coordinator_lock = threading.RLock()
_local_leases: Dict[str, dict] = {}


def default_lease_root() -> Path:
    override = os.environ.get("HERMES_CUA_LEASE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    try:
        from platformdirs import user_runtime_path

        return user_runtime_path("hermes-agent") / "computer-use"
    except Exception:
        identity_provider = getattr(
            os,
            "getuid",
            lambda: os.environ.get("USERNAME", "user"),
        )
        identity = str(identity_provider())
        return Path(tempfile.gettempdir()) / f"hermes-agent-{identity}" / "computer-use"


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _try_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class CuaOwnershipLease:
    """Process-refcounted, crash-safe CUA ownership lease."""

    def __init__(
        self,
        root: Path,
        role: str,
        *,
        wait_seconds: float = 0.0,
        handoff_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.root = Path(root)
        self.role = str(role or "interactive")
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.token = uuid.uuid4().hex
        self.handoff_callback = handoff_callback
        self._key = str(self.root.resolve())
        self._held = False

    @property
    def lock_path(self) -> Path:
        return self.root / "owner.lock"

    @property
    def metadata_path(self) -> Path:
        return self.root / "owner.json"

    @property
    def request_path(self) -> Path:
        return self.root / "handoff.json"

    def _owner_label(self) -> str:
        try:
            owner = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if isinstance(owner, dict):
                return str(owner.get("role") or "another Hermes surface")
        except (OSError, ValueError, TypeError):
            pass
        return "another Hermes surface"

    def acquire(self) -> None:
        with _coordinator_lock:
            local = _local_leases.get(self._key)
            if local is not None:
                local["refs"] += 1
                if self.handoff_callback is not None:
                    local["callbacks"].add(self.handoff_callback)
                self._held = True
                return

        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        # A tiny floor lets a same-process contender observe the coordinator
        # entry published immediately after another thread wins the kernel lock.
        deadline = time.monotonic() + max(self.wait_seconds, 0.1)
        while not _try_lock(handle):
            with _coordinator_lock:
                local = _local_leases.get(self._key)
                if local is not None:
                    handle.close()
                    local["refs"] += 1
                    if self.handoff_callback is not None:
                        local["callbacks"].add(self.handoff_callback)
                    self._held = True
                    return
            if self.role == "desktop":
                try:
                    owner = json.loads(
                        self.metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError):
                    owner = None
                if isinstance(owner, dict) and owner.get("role") == "gateway":
                    _atomic_json(
                        self.request_path,
                        {
                            "requested_at": time.time(),
                            "requester_pid": os.getpid(),
                            "target_token": owner.get("token"),
                        },
                    )
            if time.monotonic() >= deadline:
                handle.close()
                raise CuaOwnershipBusy(
                    f"Computer Use is owned by {self._owner_label()}; no "
                    "duplicate cua-driver worker was started"
                )
            time.sleep(0.05)

        metadata = {
            "acquired_at": time.time(),
            "pid": os.getpid(),
            "role": self.role,
            "schema": 1,
            "token": self.token,
        }
        try:
            _atomic_json(self.metadata_path, metadata)
            stop = threading.Event()
            with _coordinator_lock:
                # Threads in this process can race between the first local
                # lookup and kernel acquisition. Only one kernel lock can win;
                # after it does, publish the process-local refcount.
                _local_leases[self._key] = {
                    "handle": handle,
                    "callbacks": (
                        {self.handoff_callback}
                        if self.handoff_callback is not None
                        else set()
                    ),
                    "refs": 1,
                    "stop": stop,
                    "token": self.token,
                }
                self._held = True
            watcher = threading.Thread(
                target=self._watch_handoff,
                args=(self.token, stop),
                name="hermes-cua-handoff",
                daemon=True,
            )
            with _coordinator_lock:
                local = _local_leases.get(self._key)
                if local is not None:
                    local["watcher"] = watcher
            watcher.start()
        except Exception:
            _unlock(handle)
            handle.close()
            raise

    def _watch_handoff(self, token: str, stop: threading.Event) -> None:
        while not stop.wait(0.1):
            try:
                request = json.loads(
                    self.request_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError):
                continue
            if request.get("target_token") != token:
                continue
            with _coordinator_lock:
                local = _local_leases.get(self._key)
                callbacks = list(local.get("callbacks", ())) if local else []
            for callback in callbacks:
                callback()
            return

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        with _coordinator_lock:
            local = _local_leases.get(self._key)
            if local is None:
                return
            local["refs"] -= 1
            if local["refs"] > 0:
                return
            _local_leases.pop(self._key, None)
            handle = local["handle"]
            stop = local["stop"]
            token = local["token"]
            watcher = local.get("watcher")
        stop.set()
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1.0)
        try:
            try:
                owner = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                owner = None
            if isinstance(owner, dict) and owner.get("token") == token:
                self.metadata_path.unlink(missing_ok=True)
            try:
                request = json.loads(
                    self.request_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError):
                request = None
            if isinstance(request, dict) and request.get("target_token") == token:
                self.request_path.unlink(missing_ok=True)
        finally:
            try:
                _unlock(handle)
            finally:
                handle.close()
