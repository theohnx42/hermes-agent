"""Process-level contract for the Hermes maintenance barrier.

These tests intentionally exercise only the public API.  The barrier is a
process-lifetime admission gate:

* ordinary Hermes runtimes hold compatible shared leases;
* maintenance atomically publishes intent before draining those leases;
* only the exact, live maintenance transaction token may bypass the gate;
* bearer state is revalidated after ``fork()`` and never trusted from a
  process-local cache.

The implementation belongs in :mod:`hermes_cli.maintenance_barrier`; this file
is deliberately test-only so the synchronization contract can be reviewed
independently from its implementation.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import threading
import time
from pathlib import Path

import pytest

from hermes_cli.maintenance_barrier import (
    MaintenanceBarrierError,
    acquire_maintenance_lease,
    acquire_runtime_lease,
    assert_write_authorized,
    maintenance_status,
    write_authorization,
)


def _runtime_holder(
    home: str,
    events: multiprocessing.Queue,
    commands: multiprocessing.Queue,
) -> None:
    try:
        with acquire_runtime_lease(home=home, timeout=2.0):
            events.put(("acquired", os.getpid()))
            command = commands.get(timeout=10)
            if command != "release":
                raise AssertionError(f"unexpected runtime-holder command: {command!r}")
        events.put(("released", os.getpid()))
    except BaseException as exc:  # pragma: no cover - reported to parent
        events.put(("error", type(exc).__name__, str(exc)))


def _maintenance_holder(
    home: str,
    token: str,
    owner: str,
    events: multiprocessing.Queue,
    commands: multiprocessing.Queue,
) -> None:
    try:
        with acquire_maintenance_lease(
            token=token,
            owner=owner,
            home=home,
            timeout=8.0,
        ):
            events.put(("exclusive", os.getpid()))
            command = commands.get(timeout=10)
            if command != "release":
                raise AssertionError(
                    f"unexpected maintenance-holder command: {command!r}"
                )
        events.put(("released", os.getpid()))
    except BaseException as exc:  # pragma: no cover - reported to parent
        events.put(("error", type(exc).__name__, str(exc)))


def _forked_release_attempt(lease, result_fd: int) -> None:
    """Try to release a lease inherited through fork, then report the result."""

    try:
        lease.release()
        payload = b"released"
    except BaseException as exc:  # expected: lease is owned by the parent PID
        payload = f"{type(exc).__name__}:{exc}".encode()
    os.write(result_fd, payload)
    os.close(result_fd)


def _forked_cached_authorization_probe(home: str, result_fd: int) -> None:
    """A child without the explicitly carried token must revalidate and fail."""

    os.environ.pop("HERMES_MAINTENANCE_TOKEN", None)
    try:
        assert_write_authorized(home=home)
        payload = b"authorized"
    except MaintenanceBarrierError:
        payload = b"rejected"
    os.write(result_fd, payload)
    os.close(result_fd)


def _token_writer(
    home: str,
    token: str,
    events: multiprocessing.Queue,
    commands: multiprocessing.Queue,
) -> None:
    os.environ["HERMES_MAINTENANCE_TOKEN"] = token
    try:
        with write_authorization(home):
            events.put(("writing", os.getpid()))
            commands.get(timeout=10)
        events.put(("released", os.getpid()))
    except BaseException as exc:  # pragma: no cover - reported to parent
        events.put(("error", type(exc).__name__, str(exc)))


def _forked_close_then_wait(lease, ready_fd: int, wait_fd: int) -> None:
    try:
        lease.release()
    except MaintenanceBarrierError:
        pass
    os.write(ready_fd, b"closed")
    os.close(ready_fd)
    os.read(wait_fd, 1)
    os.close(wait_fd)


def _recv(events: multiprocessing.Queue, expected: str, timeout: float = 5.0):
    message = events.get(timeout=timeout)
    assert message[0] == expected, message
    return message


def _wait_for_phase(home: Path, phase: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = maintenance_status(home=home)
        if status.get("phase") == phase:
            return status
        time.sleep(0.01)
    pytest.fail(
        f"maintenance phase never became {phase!r}; "
        f"last status={maintenance_status(home=home)!r}"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock contract")
def test_multiple_normal_runtime_leases_are_shared(tmp_path: Path) -> None:
    """Independent ordinary runtimes may coexist on the same HERMES_HOME."""

    ctx = multiprocessing.get_context("spawn")
    events_a = ctx.Queue()
    events_b = ctx.Queue()
    commands_a = ctx.Queue()
    commands_b = ctx.Queue()
    proc_a = ctx.Process(
        target=_runtime_holder, args=(str(tmp_path), events_a, commands_a)
    )
    proc_b = ctx.Process(
        target=_runtime_holder, args=(str(tmp_path), events_b, commands_b)
    )
    proc_a.start()
    proc_b.start()
    try:
        _recv(events_a, "acquired")
        _recv(events_b, "acquired")
        assert maintenance_status(home=tmp_path)["phase"] == "normal"
    finally:
        commands_a.put("release")
        commands_b.put("release")
        proc_a.join(5)
        proc_b.join(5)
        if proc_a.is_alive():
            proc_a.terminate()
        if proc_b.is_alive():
            proc_b.terminate()
    assert proc_a.exitcode == 0
    assert proc_b.exitcode == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock contract")
def test_maintenance_intent_closes_admission_before_shared_holders_drain(
    tmp_path: Path,
) -> None:
    """No new writer may slip in while maintenance waits for existing runtimes."""

    ctx = multiprocessing.get_context("spawn")
    runtime_events = ctx.Queue()
    maintenance_events = ctx.Queue()
    runtime_commands = ctx.Queue()
    maintenance_commands = ctx.Queue()
    runtime = ctx.Process(
        target=_runtime_holder,
        args=(str(tmp_path), runtime_events, runtime_commands),
    )
    maintenance = ctx.Process(
        target=_maintenance_holder,
        args=(
            str(tmp_path),
            "transaction-17",
            "kestrel-release",
            maintenance_events,
            maintenance_commands,
        ),
    )
    runtime.start()
    _recv(runtime_events, "acquired")
    maintenance.start()
    try:
        status = _wait_for_phase(tmp_path, "intent")
        # Status/intent metadata must never disclose the bearer token itself.
        assert "token" not in status
        assert status["token_sha256"] == hashlib.sha256(
            b"transaction-17"
        ).hexdigest()
        assert status["owner"]["name"] == "kestrel-release"
        assert status["owner"]["pid"] == maintenance.pid
        assert status["owner"]["process_start"]

        with pytest.raises(MaintenanceBarrierError):
            acquire_runtime_lease(home=tmp_path, timeout=0.1)
        with pytest.raises(MaintenanceBarrierError):
            acquire_runtime_lease(
                home=tmp_path,
                token="wrong-transaction",
                timeout=0.1,
            )

        # The exclusive holder cannot enter until the pre-existing shared
        # holder exits, but intent already prevents all later admissions.
        with pytest.raises(queue.Empty):
            maintenance_events.get(timeout=0.1)
        runtime_commands.put("release")
        _recv(runtime_events, "released")
        _recv(maintenance_events, "exclusive")
    finally:
        maintenance_commands.put("release")
        runtime_commands.put("release")
        runtime.join(5)
        maintenance.join(5)
        if runtime.is_alive():
            runtime.terminate()
        if maintenance.is_alive():
            maintenance.terminate()
    assert runtime.exitcode == 0
    assert maintenance.exitcode == 0
    assert maintenance_status(home=tmp_path)["phase"] == "normal"


def test_exact_live_transaction_token_is_the_only_write_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate runtime may write; missing and wrong tokens may not."""

    with acquire_maintenance_lease(
        token="release-abc",
        owner="kestrel-release",
        home=tmp_path,
        timeout=1.0,
    ) as maintenance:
        maintenance.allow_candidate_writes()
        monkeypatch.delenv("HERMES_MAINTENANCE_TOKEN", raising=False)
        with pytest.raises(MaintenanceBarrierError):
            assert_write_authorized(home=tmp_path)

        monkeypatch.setenv("HERMES_MAINTENANCE_TOKEN", "release-wrong")
        with pytest.raises(MaintenanceBarrierError):
            assert_write_authorized(home=tmp_path)
        with pytest.raises(MaintenanceBarrierError):
            acquire_runtime_lease(
                home=tmp_path,
                token="release-wrong",
                timeout=0.1,
            )

        monkeypatch.setenv("HERMES_MAINTENANCE_TOKEN", "release-abc")
        assert_write_authorized(home=tmp_path)
        with acquire_runtime_lease(
            home=tmp_path,
            token="release-abc",
            timeout=0.1,
        ):
            assert_write_authorized(home=tmp_path)


def test_released_token_is_stale_and_normal_admission_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlock is recoverable, but an old transaction credential fails closed."""

    monkeypatch.setenv("HERMES_MAINTENANCE_TOKEN", "release-old")
    with acquire_maintenance_lease(
        token="release-old",
        owner="kestrel-release",
        home=tmp_path,
        timeout=1.0,
    ) as maintenance:
        maintenance.allow_candidate_writes()
        assert_write_authorized(home=tmp_path)

    assert maintenance_status(home=tmp_path)["phase"] == "normal"
    with pytest.raises(MaintenanceBarrierError):
        assert_write_authorized(home=tmp_path)
    with pytest.raises(MaintenanceBarrierError):
        acquire_runtime_lease(
            home=tmp_path,
            token="release-old",
            timeout=0.1,
        )

    monkeypatch.delenv("HERMES_MAINTENANCE_TOKEN")
    with acquire_runtime_lease(home=tmp_path, timeout=0.1):
        assert_write_authorized(home=tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow contract")
def test_barrier_rejects_symlinked_lock_and_record_paths(tmp_path: Path) -> None:
    root = tmp_path / "run" / "maintenance"
    root.mkdir(parents=True, mode=0o700)
    target = tmp_path / "outside"
    target.write_text("outside", encoding="utf-8")
    (root / "admission.lock").symlink_to(target)
    with pytest.raises(MaintenanceBarrierError):
        acquire_runtime_lease(home=tmp_path, timeout=0.1)
    (root / "admission.lock").unlink()
    with acquire_runtime_lease(home=tmp_path, timeout=0.1):
        pass
    (root / "active.json").symlink_to(target)
    with pytest.raises(MaintenanceBarrierError):
        acquire_runtime_lease(home=tmp_path, timeout=0.1)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock contract")
def test_maintenance_drains_token_writer_through_commit_boundary(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    commands = ctx.Queue()
    maintenance = acquire_maintenance_lease(
        token="candidate-write",
        owner="kestrel-release",
        home=tmp_path,
        timeout=1.0,
    )
    maintenance.allow_candidate_writes()
    writer = ctx.Process(
        target=_token_writer,
        args=(str(tmp_path), "candidate-write", events, commands),
    )
    writer.start()
    _recv(events, "writing")
    drained = threading.Event()

    def drain() -> None:
        maintenance.drain_candidate_writes(timeout=5.0)
        drained.set()

    thread = threading.Thread(target=drain)
    thread.start()
    assert not drained.wait(0.1)
    commands.put("finish")
    _recv(events, "released")
    assert drained.wait(2.0)
    thread.join(2)
    writer.join(5)
    maintenance.release()
    assert writer.exitcode == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock contract")
def test_post_drain_token_arrival_cannot_deadlock_revocation(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    commands = ctx.Queue()
    maintenance = acquire_maintenance_lease(
        token="revocation-race",
        owner="kestrel-release",
        home=tmp_path,
        timeout=1.0,
    )
    maintenance.allow_candidate_writes()
    maintenance.drain_candidate_writes(timeout=1.0)
    writer = ctx.Process(
        target=_token_writer,
        args=(str(tmp_path), "revocation-race", events, commands),
    )
    writer.start()
    with pytest.raises(queue.Empty):
        events.get(timeout=0.1)
    started = time.monotonic()
    maintenance.release()
    assert time.monotonic() - started < 0.5
    message = events.get(timeout=2.0)
    assert message[0] == "error"
    writer.join(5)
    assert writer.exitcode == 0


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires POSIX fork semantics",
)
def test_fork_cannot_release_parent_lease_or_reuse_cached_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID changes invalidate inherited lease handles and cached authorization."""

    ctx = multiprocessing.get_context("fork")
    lease = acquire_runtime_lease(home=tmp_path, timeout=0.1)
    read_fd, write_fd = os.pipe()
    child = ctx.Process(target=_forked_release_attempt, args=(lease, write_fd))
    child.start()
    os.close(write_fd)
    child_result = os.read(read_fd, 4096)
    os.close(read_fd)
    child.join(5)
    assert child.exitcode == 0
    assert child_result != b"released"

    # The inherited child's release attempt must not have dropped the
    # parent's shared lock.
    with pytest.raises(MaintenanceBarrierError):
        acquire_maintenance_lease(
            token="cannot-enter-yet",
            owner="fork-test",
            home=tmp_path,
            timeout=0.1,
        )
    lease.release()

    monkeypatch.setenv("HERMES_MAINTENANCE_TOKEN", "fork-auth")
    with acquire_maintenance_lease(
        token="fork-auth",
        owner="fork-test",
        home=tmp_path,
        timeout=1.0,
    ) as maintenance:
        maintenance.allow_candidate_writes()
        assert_write_authorized(home=tmp_path)
        read_fd, write_fd = os.pipe()
        child = ctx.Process(
            target=_forked_cached_authorization_probe,
            args=(str(tmp_path), write_fd),
        )
        child.start()
        os.close(write_fd)
        child_result = os.read(read_fd, 4096)
        os.close(read_fd)
        child.join(5)
        assert child.exitcode == 0
        assert child_result == b"rejected"


@pytest.mark.skipif(
    os.name != "posix" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires POSIX fork semantics",
)
def test_long_lived_fork_child_does_not_retain_parent_runtime_lock(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("fork")
    lease = acquire_runtime_lease(home=tmp_path, timeout=0.1)
    ready_read, ready_write = os.pipe()
    wait_read, wait_write = os.pipe()
    child = ctx.Process(
        target=_forked_close_then_wait,
        args=(lease, ready_write, wait_read),
    )
    child.start()
    os.close(ready_write)
    os.close(wait_read)
    assert os.read(ready_read, 16) == b"closed"
    os.close(ready_read)
    lease.release()
    maintenance = acquire_maintenance_lease(
        token="fork-drain",
        owner="fork-test",
        home=tmp_path,
        timeout=1.0,
    )
    maintenance.release()
    assert child.is_alive()
    os.write(wait_write, b"x")
    os.close(wait_write)
    child.join(5)
    assert child.exitcode == 0
