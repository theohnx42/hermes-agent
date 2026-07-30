"""Defense-in-depth proof that direct durable writers honor maintenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import maintenance_barrier


class _Denied(RuntimeError):
    pass


def _deny(*_args, **_kwargs) -> None:
    raise _Denied("maintenance")


@pytest.fixture
def deny_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(maintenance_barrier, "assert_write_authorized", _deny)


def test_session_state_write_is_denied_before_transaction(deny_writes) -> None:
    from hermes_state import SessionDB

    database = object.__new__(SessionDB)
    with pytest.raises(_Denied):
        database._execute_write(lambda _connection: None)


def test_kanban_creation_and_write_transaction_are_denied(
    tmp_path: Path, deny_writes
) -> None:
    from hermes_cli.kanban_db import connect, write_txn

    database = tmp_path / "new-board.db"
    with pytest.raises(_Denied):
        connect(database)
    assert not database.exists()
    with pytest.raises(_Denied):
        with write_txn(None):  # type: ignore[arg-type]
            pass


@pytest.mark.parametrize(
    "module_name",
    [
        "cron.executions",
        "gateway.delivery_ledger",
        "agent.verification_evidence",
        "tools.async_delegation",
    ],
)
def test_standalone_ledger_is_denied_before_connect(
    module_name: str, deny_writes
) -> None:
    module = __import__(module_name, fromlist=["_transaction"])
    with pytest.raises(_Denied):
        with module._transaction():
            pass
