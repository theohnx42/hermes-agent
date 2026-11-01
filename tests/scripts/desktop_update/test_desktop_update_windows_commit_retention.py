"""Cohesive Windows handoff cases; shared fixture definitions remain in the original module."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from tests.waiting import wait_until
from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import _CHECKOUT_HOLDER, _handoff


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # Synchronize with the real release wait; startup time is not lock ownership.
    holders, observers, observed, errors = [], [], {}, []
    release = tmp_path / 'release-holder'
    stopping = threading.Event()

    def hold_checkout(home: Path, install: Path) -> None:
        holder = subprocess.Popen([sys.executable, '-c', _CHECKOUT_HOLDER,
                                   str(install / '.hermes-update.lock'), str(release)])
        holders.append(holder)
        deadline = time.monotonic() + 30
        while not Path(str(release) + '.ready').exists():
            assert time.monotonic() < deadline and holder.poll() is None, 'checkout holder not ready'
            time.sleep(0.05)

        def observe_release_wait() -> None:
            try:
                deadline = time.monotonic() + 120
                log_path = home / 'logs/desktop-update-handoff.log'
                result_path = home / '.hermes-update-result.json'
                while not stopping.is_set():
                    assert time.monotonic() < deadline, 'release wait/initial result not observed'
                    assert holder.poll() is None, 'checkout holder exited before release'
                    try:
                        log = log_path.read_text(encoding='utf-8-sig')
                    except (FileNotFoundError, PermissionError):
                        log = ''
                    try:
                        initial = json.loads(result_path.read_text(encoding='utf-8-sig'))
                    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                        initial = None
                    if ('keeping the update marker' in log and isinstance(initial, dict)
                            and initial.get('ok') is True and type(initial.get('finished_at')) is int):
                        assert (home / '.hermes-update-in-progress').exists(), 'marker missing during wait'
                        observed['initial_finished_at'] = initial['finished_at']
                        # Unix-second result precision requires a distinct release second.
                        def release_clock_boundary_reached() -> bool:
                            assert time.monotonic() < deadline and not stopping.is_set(), 'release clock boundary unavailable'
                            assert holder.poll() is None, 'checkout holder exited at clock boundary'
                            return time.time() >= initial['finished_at'] + 1

                        wait_until(release_clock_boundary_reached, timeout=max(0, deadline - time.monotonic()),
                                   what='release clock boundary unavailable')
                        observed['release_epoch'] = int(time.time())
                        release.touch()
                        return
                    time.sleep(0.05)
            except BaseException as exc:
                errors.append(exc)
            finally:
                # A failed observation must not strand the fixture handoff for two hours.
                release.touch()

        observer = threading.Thread(target=observe_release_wait, daemon=True)
        observers.append(observer)
        observer.start()

    try:
        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
    finally:
        stopping.set()
        release.touch()
        for observer in observers:
            observer.join(timeout=5)
        for holder in holders:
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=10)
    assert all(not observer.is_alive() for observer in observers), 'fixture observer did not stop'
    assert not errors, errors
    assert 'release_epoch' in observed, 'real release wait was not observed'
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert result['ok'] is True, result
    assert result['finished_at'] > observed['initial_finished_at'], (result, observed)
    assert result['finished_at'] >= observed['release_epoch'], (result, observed)


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)
