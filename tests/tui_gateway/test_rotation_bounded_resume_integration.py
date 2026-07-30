from __future__ import annotations

import json
import threading

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start


class _DormantThread:
    """Capture scheduled recovery without starting an agent in this unit test."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self.target = target

    def start(self):
        return None


def test_rotation_tip_is_bounded_resume_target_with_continuity_metadata(
    monkeypatch, tmp_path
):
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = "rotation-parent"
    child = "rotation-tip"
    title = "Exact investigation title"
    goal = {
        "goal": "finish without losing continuity",
        "status": "active",
        "subgoals": ["retain pending work", "keep the exact tip"],
    }
    db.create_session(parent, source="webui", cwd="/work/project")
    db.set_session_title(parent, title)
    db.set_meta(f"goal:{parent}", json.dumps(goal))
    for index in range(1_199):
        db.append_message(
            parent,
            "user" if index % 2 == 0 else "assistant",
            f"event-{index}",
        )

    assert db.try_acquire_compression_lock(parent, "winner", ttl_seconds=60)
    db.publish_compression_child(
        parent_session_id=parent,
        child_session_id=child,
        source="webui",
        messages=[{"role": "user", "content": "event-1199"}],
        compression_lock_holder="winner",
        local_rotation=True,
        lineage_compression_count=3,
    )

    record_turn_start(tmp_path, child, "finish the pending turn", attempts=1)
    interrupted_at = read_turn_marker(tmp_path, child)
    assert interrupted_at is not None
    server._sessions.clear()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 3_600, 3))
    monkeypatch.setattr(server.threading, "Thread", _DormantThread)

    response = server.handle_request(
        {
            "id": "resume-rotation",
            "method": "session.resume",
            "params": {"session_id": parent, "history_limit": 500},
        }
    )
    result = response["result"]

    assert result["resumed"] == child
    assert result["session_key"] == child
    assert result["message_count"] == 1_200
    assert len(result["messages"]) == 500
    assert result["messages"][0]["text"] == "event-700"
    assert result["messages"][-1]["text"] == "event-1199"
    assert result["history_window"] == {
        "has_more_before": True,
        "limit": 500,
        "start": 700,
        "total": 1_200,
    }
    tip_texts = {message["text"] for message in result["messages"]}
    assert len(tip_texts) == 500
    older_page = db.get_messages_as_conversation(child, include_ancestors=True)[
        200:700
    ]
    assert older_page[-1]["content"] == "event-699"
    assert tip_texts.isdisjoint(message["content"] for message in older_page)
    assert result["auto_continue"]["attempt"] == 2
    assert result["auto_continue"]["interrupted_at"] == interrupted_at["started_at"]
    assert read_turn_marker(tmp_path, child) == interrupted_at

    tip = db.get_session(child)
    assert tip["title"] == title
    assert json.loads(db.get_meta(f"goal:{child}")) == goal
    assert json.loads(db.get_meta(f"goal:{parent}"))["status"] == "cleared"
