"""Regression tests for stale writes after a compression session split."""

from __future__ import annotations

import json

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _compression_parent(db: SessionDB, session_id: str = "parent") -> None:
    db.create_session(session_id, source="webui")
    db.append_message(session_id, "user", "before split")
    db.end_session(session_id, "compression")


def test_find_live_compression_child_returns_unique_direct_child(db: SessionDB) -> None:
    _compression_parent(db)
    db.create_session("child", source="webui", parent_session_id="parent")

    child = db.find_live_compression_child("parent")

    assert child is not None
    assert child["id"] == "child"
    assert child["parent_session_id"] == "parent"
    assert child["ended_at"] is None


def test_find_live_compression_child_fails_closed_when_ambiguous(db: SessionDB) -> None:
    _compression_parent(db)
    db.create_session("child-a", source="webui", parent_session_id="parent")
    db.create_session("child-b", source="webui", parent_session_id="parent")

    assert db.find_live_compression_child("parent") is None


def test_find_live_compression_child_ignores_ended_children(db: SessionDB) -> None:
    _compression_parent(db)
    db.create_session("ended-child", source="webui", parent_session_id="parent")
    db.end_session("ended-child", "agent_close")

    assert db.find_live_compression_child("parent") is None


def test_find_live_compression_child_ignores_non_continuation_children(
    db: SessionDB,
) -> None:
    _compression_parent(db)
    db.create_session("canonical", source="webui", parent_session_id="parent")
    db.create_session(
        "branch",
        source="webui",
        parent_session_id="parent",
        model_config={"_branched_from": "parent"},
    )
    db.create_session(
        "delegate",
        source="webui",
        parent_session_id="parent",
        model_config={"_delegate_from": "parent"},
    )
    db.create_session("tool-child", source="tool", parent_session_id="parent")

    child = db.find_live_compression_child("parent")

    assert child is not None
    assert child["id"] == "canonical"


def test_append_message_rejects_compression_ended_parent_atomically(db: SessionDB) -> None:
    _compression_parent(db)
    before = db.get_session("parent")["message_count"]

    with pytest.raises(RuntimeError, match="closed by compression"):
        db.append_message("parent", "assistant", "must not land on parent")

    assert db.get_session("parent")["message_count"] == before
    assert [m["content"] for m in db.get_messages("parent")] == ["before split"]


def test_append_message_preserves_legacy_behavior_for_other_end_reasons(db: SessionDB) -> None:
    db.create_session("ended", source="test")
    db.end_session("ended", "agent_close")

    message_id = db.append_message("ended", "user", "legacy append")

    assert isinstance(message_id, int)
    assert db.get_messages("ended")[-1]["content"] == "legacy append"


def test_replace_messages_rejects_compression_ended_parent_atomically(
    db: SessionDB,
) -> None:
    _compression_parent(db)

    with pytest.raises(RuntimeError, match="closed by compression"):
        db.replace_messages("parent", [{"role": "user", "content": "rewrite"}])

    assert [m["content"] for m in db.get_messages("parent")] == ["before split"]


def test_publish_compression_child_is_atomic_on_handoff_failure(
    db: SessionDB, monkeypatch
) -> None:
    db.create_session("atomic-parent", source="webui")
    db.append_message("atomic-parent", "user", "original")
    assert db.try_acquire_compression_lock("atomic-parent", "winner", ttl_seconds=60)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("handoff insert failed")

    monkeypatch.setattr(db, "_insert_message_rows", _boom)
    with pytest.raises(RuntimeError, match="handoff insert failed"):
        db.publish_compression_child(
            parent_session_id="atomic-parent",
            child_session_id="atomic-child",
            source="webui",
            messages=[{"role": "user", "content": "summary"}],
            compression_lock_holder="winner",
        )

    parent = db.get_session("atomic-parent")
    assert parent is not None
    assert parent["ended_at"] is None
    assert db.get_session("atomic-child") is None


def test_publish_compression_child_exposes_complete_child(db: SessionDB) -> None:
    db.create_session("atomic-parent", source="webui")
    db.append_message("atomic-parent", "user", "original")
    assert db.try_acquire_compression_lock("atomic-parent", "winner", ttl_seconds=60)

    db.publish_compression_child(
        parent_session_id="atomic-parent",
        child_session_id="atomic-child",
        source="webui",
        system_prompt="compressed system",
        messages=[{"role": "user", "content": "summary"}],
        compression_lock_holder="winner",
    )

    assert db.get_session("atomic-parent")["end_reason"] == "compression"
    child = db.find_live_compression_child("atomic-parent")
    assert child is not None
    assert child["id"] == "atomic-child"
    assert child["system_prompt"] == "compressed system"
    assert [m["content"] for m in db.get_messages("atomic-child")] == ["summary"]


def test_publish_compression_child_rejects_lost_or_expired_lease(db: SessionDB) -> None:
    db.create_session("lease-parent", source="webui")
    db.append_message("lease-parent", "user", "new durable turn")
    assert db.try_acquire_compression_lock("lease-parent", "new-winner", ttl_seconds=60)

    with pytest.raises(RuntimeError, match="lease lost"):
        db.publish_compression_child(
            parent_session_id="lease-parent",
            child_session_id="stale-child",
            source="webui",
            messages=[{"role": "user", "content": "stale summary"}],
            compression_lock_holder="old-loser",
        )

    parent = db.get_session("lease-parent")
    assert parent is not None
    assert parent["ended_at"] is None
    assert db.get_session("stale-child") is None
    assert [m["content"] for m in db.get_messages("lease-parent")] == [
        "new durable turn"
    ]


def test_local_rotation_atomically_moves_continuity_state(db: SessionDB) -> None:
    db.create_session(
        "rotation-parent",
        source="webui",
        model_config={"profile": "condor"},
        cwd="/work/project",
        profile_name="condor",
    )
    db.set_session_title("rotation-parent", "active investigation")
    goal = {
        "goal": "finish the investigation",
        "status": "active",
        "subgoals": ["preserve decisions", "close open loops"],
    }
    db.set_meta("goal:rotation-parent", json.dumps(goal))
    db.set_meta("compression-lineage-count:rotation-parent", "2")
    assert db.try_acquire_compression_lock(
        "rotation-parent", "winner", ttl_seconds=60
    )

    db.publish_compression_child(
        parent_session_id="rotation-parent",
        child_session_id="rotation-child",
        source="webui",
        model_config={
            "_local_rotation_handoff": {
                "preserves": ["decisions", "open_loops"],
            }
        },
        messages=[
            {
                "role": "user",
                "content": (
                    "## Goal\nfinish the investigation\n"
                    "## Active State\nworking tree preserved\n"
                    "## Blocked\none open loop\n"
                    "## Key Decisions\nkeep the atomic boundary"
                ),
            }
        ],
        compression_lock_holder="winner",
        local_rotation=True,
        lineage_compression_count=3,
    )

    parent = db.get_session("rotation-parent")
    child = db.get_session("rotation-child")
    assert parent["ended_at"] is not None
    assert parent["end_reason"] == "compression"
    assert parent["title"] is None
    assert child["parent_session_id"] == "rotation-parent"
    assert child["title"] == "active investigation"
    assert child["cwd"] == "/work/project"
    assert child["profile_name"] == "condor"
    child_model_config = json.loads(child["model_config"])
    assert child_model_config["_local_rotation_handoff"]["preserves"] == [
        "decisions",
        "open_loops",
    ]
    assert json.loads(db.get_meta("goal:rotation-parent"))["status"] == "cleared"
    assert json.loads(db.get_meta("goal:rotation-child")) == goal
    assert db.get_compression_lineage_count("rotation-parent") == 3
    assert db.get_compression_lineage_count("rotation-child") == 0


def test_local_rotation_last_boundary_failure_rolls_everything_back(
    db: SessionDB,
) -> None:
    """A crash/error at the final parent-close boundary exposes only old state."""
    db.create_session("rollback-parent", source="webui", cwd="/work/project")
    db.set_session_title("rollback-parent", "rollback proof")
    goal = {"goal": "do not lose me", "status": "active"}
    db.set_meta("goal:rollback-parent", json.dumps(goal))
    db.set_meta("compression-lineage-count:rollback-parent", "2")
    assert db.try_acquire_compression_lock(
        "rollback-parent", "winner", ttl_seconds=60
    )
    db._conn.execute(
        """
        CREATE TRIGGER fail_local_rotation_parent_close
        BEFORE UPDATE OF ended_at ON sessions
        WHEN NEW.id = 'rollback-parent' AND NEW.ended_at IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, 'simulated crash at parent close');
        END
        """
    )

    with pytest.raises(Exception, match="simulated crash at parent close"):
        db.publish_compression_child(
            parent_session_id="rollback-parent",
            child_session_id="rollback-child",
            source="webui",
            messages=[{"role": "user", "content": "structured handoff"}],
            compression_lock_holder="winner",
            local_rotation=True,
            lineage_compression_count=3,
        )

    parent = db.get_session("rollback-parent")
    assert parent["ended_at"] is None
    assert parent["title"] == "rollback proof"
    assert db.get_session("rollback-child") is None
    assert json.loads(db.get_meta("goal:rollback-parent")) == goal
    assert db.get_meta("goal:rollback-child") is None
    assert db.get_compression_lineage_count("rollback-parent") == 2


def test_compression_lease_blocks_non_owner_but_allows_owner_flush(
    db: SessionDB,
) -> None:
    db.create_session("leased", source="webui")
    assert db.try_acquire_compression_lock("leased", "winner", ttl_seconds=60)

    with pytest.raises(RuntimeError, match="being compressed"):
        db.append_message("leased", "user", "late stale turn")

    db.append_message(
        "leased",
        "assistant",
        "winner flush",
        compression_lock_holder="winner",
    )
    assert [m["content"] for m in db.get_messages("leased")] == ["winner flush"]
