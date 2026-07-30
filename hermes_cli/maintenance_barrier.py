"""Process-wide writer exclusion for lossless Hermes release transactions.

Normal Hermes entrypoints hold a shared runtime lease for their lifetime.
An activator first publishes a maintenance intent under the admission lock,
which prevents new normal runtimes from entering, then drains existing shared
holders by acquiring the runtime lock exclusively.  Candidate processes may
run under that exclusive lease only with the exact token recorded by the live
maintenance owner.

The barrier is currently enforced on POSIX, which covers the macOS and Linux
release adapters.  Other platforms retain their existing behavior until their
native adapter supplies an equivalent kernel-backed lock.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from functools import wraps
from typing import IO, Callable, Iterator, Optional, TypeVar

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows adapter owns its native gate
    fcntl = None  # type: ignore[assignment]

from hermes_constants import get_hermes_home


TOKEN_ENV = "HERMES_MAINTENANCE_TOKEN"
_PROCESS_LEASE: Optional["BarrierLease"] = None
_PROCESS_PID: Optional[int] = None
T = TypeVar("T")


class MaintenanceBarrierError(RuntimeError):
    """Raised when maintenance safely excludes this Hermes process."""


def _paths(home: Path) -> tuple[Path, Path, Path, Path]:
    root = home / "run" / "maintenance"
    return (
        root / "admission.lock",
        root / "runtime.lock",
        root / "writers.lock",
        root / "active.json",
    )


def _home(home: Path | str | None) -> Path:
    return Path(home or get_hermes_home()).expanduser().resolve()


def _secure_root(home: Path) -> Path:
    root = home / "run" / "maintenance"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise MaintenanceBarrierError(
            f"unsafe Hermes maintenance directory: {root}"
        )
    os.chmod(root, 0o700)
    return root


def _open_lock(path: Path) -> IO[bytes]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise MaintenanceBarrierError(
            f"cannot safely open Hermes maintenance lock {path}: {error}"
        ) from error
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise MaintenanceBarrierError(f"unsafe Hermes maintenance lock: {path}")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a+b")


def _process_start(pid: int) -> str:
    if Path(f"/proc/{pid}/stat").is_file():
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return fields[21] if len(fields) > 21 else ""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _owner_is_live(record: dict[str, object]) -> bool:
    try:
        pid = int(record["owner_pid"])
        expected = str(record["owner_started"])
        os.kill(pid, 0)
    except (KeyError, TypeError, ValueError, ProcessLookupError, PermissionError):
        return False
    return bool(expected) and secrets.compare_digest(_process_start(pid), expected)


def _flock(handle: IO[bytes], operation: int, timeout: float) -> None:
    if fcntl is None:
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                raise MaintenanceBarrierError("Hermes maintenance lease timed out")
            time.sleep(0.05)


def _unlock(handle: IO[bytes]) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_record(path: Path) -> dict[str, object] | None:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MaintenanceBarrierError(
            f"cannot safely open Hermes maintenance record: {error}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise MaintenanceBarrierError(
                f"unsafe Hermes maintenance record: {path}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise MaintenanceBarrierError(
            f"invalid Hermes maintenance record: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise MaintenanceBarrierError("invalid Hermes maintenance record shape")
    return payload


def _write_record(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


@dataclass
class BarrierLease:
    home: Path
    kind: str
    handle: IO[bytes] | None
    token: str | None = None
    record_path: Path | None = None
    owner_pid: int = 0
    writer_handle: IO[bytes] | None = None
    admission_handle: IO[bytes] | None = None
    _closed: bool = False

    def acquire_exclusive(self, timeout: float = 60.0) -> "BarrierLease":
        if self.kind != "maintenance-intent" or self._closed:
            raise MaintenanceBarrierError(
                "exclusive drain requires a live maintenance intent"
            )
        admission, runtime, writers, _active = _paths(self.home)
        admission_handle = _open_lock(admission)
        handle: IO[bytes] | None = None
        writer_handle: IO[bytes] | None = None
        try:
            _flock(
                admission_handle,
                fcntl.LOCK_EX if fcntl else 0,
                timeout,
            )
            handle = _open_lock(runtime)
            writer_handle = _open_lock(writers)
            _flock(handle, fcntl.LOCK_EX if fcntl else 0, timeout)
            _flock(
                writer_handle,
                fcntl.LOCK_EX if fcntl else 0,
                timeout,
            )
        except BaseException:
            if writer_handle is not None:
                writer_handle.close()
            if handle is not None:
                handle.close()
            _unlock(admission_handle)
            admission_handle.close()
            raise
        self.admission_handle = admission_handle
        self.handle = handle
        self.writer_handle = writer_handle
        self.kind = "maintenance"
        return self

    def allow_candidate_writes(self) -> None:
        """Release only the writer drain while retaining maintenance intent."""
        if self.kind != "maintenance" or self._closed:
            raise MaintenanceBarrierError("maintenance is not exclusively held")
        if self.writer_handle is not None:
            _unlock(self.writer_handle)
            self.writer_handle.close()
            self.writer_handle = None
        if self.admission_handle is not None:
            _unlock(self.admission_handle)
            self.admission_handle.close()
            self.admission_handle = None

    def drain_candidate_writes(self, timeout: float = 60.0) -> None:
        """Prevent new token writes and wait for all in-flight writes."""
        if self.kind != "maintenance" or self._closed:
            raise MaintenanceBarrierError("maintenance is not exclusively held")
        if self.writer_handle is not None:
            return
        admission, _runtime, writers, _active = _paths(self.home)
        admission_handle = _open_lock(admission)
        handle: IO[bytes] | None = None
        try:
            _flock(
                admission_handle,
                fcntl.LOCK_EX if fcntl else 0,
                timeout,
            )
            handle = _open_lock(writers)
            _flock(handle, fcntl.LOCK_EX if fcntl else 0, timeout)
        except BaseException:
            if handle is not None:
                handle.close()
            _unlock(admission_handle)
            admission_handle.close()
            raise
        self.admission_handle = admission_handle
        self.writer_handle = handle

    def release(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.owner_pid and os.getpid() != self.owner_pid:
            # A fork inherits the parent's open file description. Closing the
            # child's duplicate without LOCK_UN preserves the parent's flock.
            if self.writer_handle is not None:
                self.writer_handle.close()
                self.writer_handle = None
            if self.admission_handle is not None:
                self.admission_handle.close()
                self.admission_handle = None
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            raise MaintenanceBarrierError(
                "forked process cannot release its parent's Hermes lease"
            )
        if self.kind == "maintenance" and self.writer_handle is None:
            self.drain_candidate_writes()
        if self.kind in {"maintenance", "maintenance-intent"} and self.record_path is not None:
            admission, _runtime, _writers, active = _paths(self.home)
            admission_handle = self.admission_handle or _open_lock(admission)
            acquired_here = self.admission_handle is None
            try:
                if acquired_here:
                    _flock(
                        admission_handle,
                        fcntl.LOCK_EX if fcntl else 0,
                        10.0,
                    )
                current = _read_record(active)
                if (
                    current
                    and int(current.get("owner_pid", -1)) == self.owner_pid
                    and secrets.compare_digest(
                        str(current.get("token_sha256", "")),
                        hashlib.sha256((self.token or "").encode()).hexdigest(),
                    )
                ):
                    active.unlink(missing_ok=True)
            finally:
                if acquired_here:
                    _unlock(admission_handle)
                    admission_handle.close()
        if self.writer_handle is not None:
            _unlock(self.writer_handle)
            self.writer_handle.close()
            self.writer_handle = None
        if self.handle is not None:
            _unlock(self.handle)
            self.handle.close()
            self.handle = None
        if self.admission_handle is not None:
            _unlock(self.admission_handle)
            self.admission_handle.close()
            self.admission_handle = None
        self._closed = True

    def __enter__(self) -> "BarrierLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _validated_token(active: Path, token: str | None) -> str | None:
    record = _read_record(active)
    if record is None:
        if token:
            raise MaintenanceBarrierError(
                "stale Hermes maintenance token without an active transaction"
            )
        return None
    if not _owner_is_live(record):
        raise MaintenanceBarrierError(
            "Hermes maintenance owner record is stale; operator recovery required"
        )
    digest = hashlib.sha256((token or "").encode()).hexdigest()
    if token and secrets.compare_digest(
        str(record.get("token_sha256", "")), digest
    ):
        return token
    raise MaintenanceBarrierError("Hermes is in an active maintenance transaction")


def acquire_runtime_lease(
    home: Path | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> BarrierLease:
    resolved = _home(home)
    admission, runtime, _writers, active = _paths(resolved)
    _secure_root(resolved)
    admission_handle = _open_lock(admission)
    runtime_handle: IO[bytes] | None = None
    try:
        _flock(admission_handle, fcntl.LOCK_SH if fcntl else 0, timeout)
        supplied = token if token is not None else os.environ.get(TOKEN_ENV)
        authorized = _validated_token(active, supplied)
        if authorized:
            return BarrierLease(resolved, "token", None, authorized)
        runtime_handle = _open_lock(runtime)
        _flock(runtime_handle, fcntl.LOCK_SH if fcntl else 0, timeout)
        return BarrierLease(
            resolved, "runtime", runtime_handle, owner_pid=os.getpid()
        )
    except BaseException:
        if runtime_handle is not None:
            runtime_handle.close()
        raise
    finally:
        _unlock(admission_handle)
        admission_handle.close()


def acquire_maintenance_lease(
    token: str | None = None,
    owner: str = "hermes-release-activation",
    home: Path | None = None,
    timeout: float = 60.0,
) -> BarrierLease:
    lease = begin_maintenance_intent(token, owner, home, timeout)
    try:
        return lease.acquire_exclusive(timeout)
    except BaseException:
        lease.close()
        raise


def begin_maintenance_intent(
    token: str | None = None,
    owner: str = "hermes-release-activation",
    home: Path | str | None = None,
    timeout: float = 10.0,
) -> BarrierLease:
    """Close runtime admission now; drain existing holders separately."""
    if fcntl is None:
        raise MaintenanceBarrierError(
            "native maintenance locking is unavailable on this platform"
        )
    resolved = _home(home)
    admission, _runtime, _writers, active = _paths(resolved)
    _secure_root(resolved)
    token = token or secrets.token_urlsafe(32)
    admission_handle = _open_lock(admission)
    published = False
    try:
        _flock(admission_handle, fcntl.LOCK_EX, timeout)
        if _read_record(active) is not None:
            raise MaintenanceBarrierError(
                "another Hermes maintenance transaction is active"
            )
        record = {
            "schema_version": 1,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "owner": {
                "name": owner,
                "pid": os.getpid(),
                "process_start": _process_start(os.getpid()),
            },
            "owner_pid": os.getpid(),
            "owner_started": _process_start(os.getpid()),
            "created_at": time.time(),
            "home": str(resolved),
        }
        if not record["owner_started"]:
            raise MaintenanceBarrierError("cannot prove maintenance owner identity")
        _write_record(active, record)
        published = True
        return BarrierLease(
            resolved,
            "maintenance-intent",
            None,
            token,
            active,
            os.getpid(),
        )
    except BaseException:
        if published:
            active.unlink(missing_ok=True)
        raise
    finally:
        _unlock(admission_handle)
        admission_handle.close()


def assert_write_authorized(home: Path | None = None) -> None:
    """Reject a write unless this process owns a valid runtime admission."""
    global _PROCESS_LEASE, _PROCESS_PID
    pid = os.getpid()
    if _PROCESS_PID != pid:
        if _PROCESS_LEASE is not None:
            if _PROCESS_LEASE.writer_handle is not None:
                _PROCESS_LEASE.writer_handle.close()
                _PROCESS_LEASE.writer_handle = None
            if _PROCESS_LEASE.handle is not None:
                _PROCESS_LEASE.handle.close()
            _PROCESS_LEASE.handle = None
        _PROCESS_LEASE = None
        _PROCESS_PID = pid
    resolved = _home(home)
    if (
        _PROCESS_LEASE is None
        or _PROCESS_LEASE._closed
        or _PROCESS_LEASE.home != resolved
        or _PROCESS_LEASE.kind == "token"
    ):
        if _PROCESS_LEASE is not None and not _PROCESS_LEASE._closed:
            _PROCESS_LEASE.close()
        _PROCESS_LEASE = acquire_runtime_lease(resolved)
        atexit.register(_PROCESS_LEASE.close)


def enter_process_runtime(home: Path | None = None) -> BarrierLease:
    """Acquire and retain this process's canonical Hermes runtime admission."""
    assert_write_authorized(home)
    assert _PROCESS_LEASE is not None
    return _PROCESS_LEASE


@contextlib.contextmanager
def write_authorization(
    home: Path | str | None = None, timeout: float = 60.0
) -> Iterator[None]:
    """Hold kernel-backed authorization through a complete durable mutation."""
    resolved = _home(home)
    assert_write_authorized(resolved)
    assert _PROCESS_LEASE is not None
    if _PROCESS_LEASE.kind != "token":
        yield
        return
    admission, _runtime, writers, active = _paths(resolved)
    _secure_root(resolved)
    admission_handle = _open_lock(admission)
    writer_handle: IO[bytes] | None = None
    try:
        _flock(admission_handle, fcntl.LOCK_SH if fcntl else 0, timeout)
        _validated_token(active, os.environ.get(TOKEN_ENV))
        writer_handle = _open_lock(writers)
        _flock(writer_handle, fcntl.LOCK_SH if fcntl else 0, timeout)
    finally:
        _unlock(admission_handle)
        admission_handle.close()
    try:
        yield
    finally:
        if writer_handle is not None:
            _unlock(writer_handle)
            writer_handle.close()


def write_authorized(function: Callable[..., T]) -> Callable[..., T]:
    """Decorator retaining writer authorization for the whole call."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with write_authorization():
            return function(*args, **kwargs)

    return wrapper


def write_authorized_generator(function):
    """Generator decorator retaining authorization across every yield."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with write_authorization():
            yield from function(*args, **kwargs)

    return wrapper


def maintenance_status(home: Path | None = None) -> dict[str, object]:
    """Return non-secret operator status for the active barrier."""
    resolved = _home(home)
    _admission, _runtime, _writers, active = _paths(resolved)
    record = _read_record(active)
    if record is None:
        return {"phase": "normal", "home": str(resolved)}
    return {
        "phase": "intent",
        "home": str(resolved),
        "token_sha256": str(record.get("token_sha256", "")),
        "owner": record.get("owner", {}),
        "created_at": record.get("created_at"),
        "live": _owner_is_live(record),
    }
