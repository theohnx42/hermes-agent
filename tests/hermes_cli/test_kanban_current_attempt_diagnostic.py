"""Append-mode logs must not attribute prior attempts to a current failure."""
import pytest
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.mark.parametrize("trailer", ["\n[kanban-worker-exit] rc=1\n", ""])
def test_current_transport_error_survives_old_resume_hint(monkeypatch, trailer):
    old = ("Old hold until 2026-10-20\nResume this session with:\n"
           "hermes --resume old-session\n[kanban-worker-exit] rc=0\n")
    current = ("Session: local-current\n"
               "Kanban owner refused: Gateway connection failed; no local fallback\n")
    monkeypatch.setattr(dispatch._kb, "read_worker_log", lambda *_a, **_k: old + current + trailer)
    monkeypatch.setattr(dispatch, "_exit_summary_marker", lambda: "Resume this session with:")
    monkeypatch.setattr(dispatch, "_log_noise_prefixes", lambda: ("Query:",))
    result = dispatch._worker_final_output("task", board="owned-board")
    assert "Gateway connection failed; no local fallback" in result
    assert "hold until" not in result
    assert "old-session" not in result


def test_single_legacy_attempt_still_trims_exit_summary(monkeypatch):
    raw = ("Current provider failure\nResume this session with:\n"
           "hermes --resume current\n[kanban-worker-exit] rc=1\n")
    monkeypatch.setattr(dispatch._kb, "read_worker_log", lambda *_a, **_k: raw)
    monkeypatch.setattr(dispatch, "_exit_summary_marker", lambda: "Resume this session with:")
    monkeypatch.setattr(dispatch, "_log_noise_prefixes", lambda: ())
    assert dispatch._worker_final_output("task", board="owned-board") == "Current provider failure"
