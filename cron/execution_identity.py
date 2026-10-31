"""Which cron execution the current code runs inside, for plugins (``ctx.current_cron_execution()``).

The scheduler binds it once it has won the execution's ``claimed`` → ``running`` transition (or a
restart-safe worker adopted it) and resets it when the fire ends. It is a ContextVar, so it follows
the agent onto the threads the scheduler already runs it on via ``contextvars.copy_context()`` and
never leaks to another concurrently firing job or profile. Model output cannot reach it: tool
subprocesses do not share the interpreter. Delegated children read ``None`` — they are not the
scheduled occurrence.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class CronExecution:
    job_id: str
    job_name: str
    execution_id: str
    source: str  # "builtin" (ticker), "direct" (manual run / dashboard Run now), provider name...
    scheduled_instant: Optional[str]  # the occurrence fired; None for an off-schedule run
    started_at: Optional[str]
    profile: str


_BOOKKEEPING_HOME: ContextVar[Optional[Path]] = ContextVar("hermes_cron_bookkeeping_home", default=None)


_CURRENT: ContextVar[Optional[CronExecution]] = ContextVar("hermes_cron_execution", default=None)


def enter_cron_execution(job: Mapping[str, Any], execution_id: str, record: Mapping[str, Any]) -> Token:
    """Bind this fire's identity from *record*, the ledger row of the running transition."""
    from hermes_cli.profiles import current_profile_name

    return _CURRENT.set(CronExecution(
        job_id=str(job["id"]),
        job_name=str(job.get("name") or job["id"]),
        execution_id=str(execution_id),
        source=str(record.get("source") or ""),
        scheduled_instant=record.get("scheduled_instant"),
        started_at=record.get("started_at"),
        profile=current_profile_name("default") or "default",
    ))


def exit_cron_execution(token: Optional[Token]) -> None:
    if token is not None:
        _CURRENT.reset(token)


def current_cron_execution() -> Optional[CronExecution]:
    from agent.delegation_context import is_delegated_child_context

    return None if is_delegated_child_context() else _CURRENT.get()


def normalize_execution_profile(value: Any) -> Optional[str]:
    """Canonical existing profile name; never a caller-supplied filesystem path."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("execution_profile must be an existing profile name")
    from hermes_cli.profiles import normalize_profile_name, get_profile_dir, profile_exists
    name = normalize_profile_name(value)
    home = get_profile_dir(name)
    if not profile_exists(name) or not home.is_dir():
        raise ValueError("execution_profile does not exist")
    return name


def execution_profile_home(name: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir(normalize_execution_profile(name)).resolve()


def cron_child_store_binding() -> Optional[dict]:
    """Non-secret, native-run-bound store context for this executor's terminal subprocesses."""
    from gateway.session_cron import current_execution
    from cron.jobs import get_cron_store_home
    from hermes_constants import profile_name_for_home
    execution, native = current_cron_execution(), current_execution()
    store, target = get_cron_store_home(), get_hermes_home().resolve()
    if execution is None or store == target:
        return None
    if (native is None or native[2:] != (execution.job_id, execution.execution_id)
            or Path(native[0].db.db_path).resolve().parent != target):
        raise ValueError("native cron execution context unavailable")
    from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
    from hermes_state_runtime import find_session_admission, _epoch
    with native[0].db._read_ctx() as conn:
        _epoch(conn, native[0].epoch)
    row = find_session_admission(native[0].db, principal_id=CRON_PRINCIPAL_ID,
                                 session_id=native[1], request_id=cron_request_id(execution.job_id, execution.execution_id))
    if row is None or row['status'] != 'started' or row['owner_epoch'] != native[0].epoch:
        raise ValueError("native cron execution context unavailable")
    return {"version": 1, "admission_id": row['admission_id'],
            "generation": row['generation'], "owner_epoch": row['owner_epoch'], "store_profile": profile_name_for_home(store) or "default",
            "store_home": str(store), "execution_home": str(target),
            "execution_profile": profile_name_for_home(target) or "default",
            "job_id": execution.job_id, "execution_id": execution.execution_id,
            "session_id": native[1]}


def _require_child_native_binding(binding, target, store):
    """Read the existing immutable native receipt, not today's editable job assignment."""
    from hermes_state import SessionDB
    from hermes_state_keys import CRON_PRINCIPAL_ID, cron_request_id
    from hermes_state_local import local_receipt
    from hermes_state_runtime import get_session_admission, RuntimeStoreError, _epoch
    import sqlite3
    if not (target / 'state.db').is_file():
        raise ValueError("native cron store binding unavailable")
    db = None
    try:
        db = SessionDB(db_path=target / 'state.db', read_only=True)
        receipt = local_receipt(db, binding['session_id'])
        frozen = json.loads(receipt['policy']['request_json'])
        request = cron_request_id(binding['job_id'], binding['execution_id'])
        expected = {k: binding[k] for k in ('store_profile', 'store_home', 'execution_profile', 'execution_home')}
        if (Path(receipt['profile_id']).resolve() != target
                or receipt['session_id'] != binding['session_id']
                or receipt['principal_id'] != CRON_PRINCIPAL_ID or receipt['request_id'] != request
                or frozen['request_id'] != request or frozen['cron_job']['id'] != binding['job_id']
                or any(frozen.get(k) != v for k, v in expected.items()) or str(store) != frozen['store_home']):
            raise ValueError("native cron store binding mismatch")
        with db._read_ctx() as conn:
            _epoch(conn, binding['owner_epoch'])
        row = get_session_admission(db, admission_id=binding['admission_id'])
        if (row is None or row['target_session_id'] != binding['session_id']
                or row['request_id'] != request or row['principal_id'] != CRON_PRINCIPAL_ID
                or row['status'] != 'started' or row['generation'] != binding['generation']
                or row['owner_epoch'] != binding['owner_epoch']):
            raise ValueError("native cron admission is not executing")
    except (OSError, KeyError, TypeError, ValueError, RuntimeStoreError, sqlite3.Error):
        raise ValueError("native cron store binding unavailable") from None
    finally:
        if db is not None:
            db.close()


@contextlib.contextmanager
def child_notepad_store(job_id: str):
    """Validate the frozen native admission and central claim before exposing its notepad."""
    # health: allow HX002 -- non-secret native admission transport, never a user setting; inherited values are stripped at spawn and canonical homes, started admission/generation/epoch plus the live central claim are validated before selecting any store
    raw = os.environ.get("HERMES_CRON_STORE_BINDING")
    if not raw:
        yield
        return
    binding = json.loads(raw)
    keys = {"version", "store_profile", "store_home", "execution_profile", "execution_home",
            "job_id", "execution_id", "session_id", "admission_id", "generation", "owner_epoch"}
    integer_keys = {'generation', 'owner_epoch'}
    if (not isinstance(binding, dict) or set(binding) != keys
            or type(binding['version']) is not int or binding['version'] != 1
            or any(type(binding[k]) is not int or binding[k] <= 0 for k in integer_keys)
            or any(not isinstance(binding[k], str) or not binding[k] for k in keys - integer_keys - {'version'})
            or binding['job_id'] != job_id):
        raise ValueError("invalid cron store binding")
    target, store = execution_profile_home(binding['execution_profile']), execution_profile_home(binding['store_profile'])
    if (target != get_hermes_home().resolve() or str(target) != binding['execution_home']
            or str(store) != binding['store_home']):
        raise ValueError("cron executor identity mismatch")
    _require_child_native_binding(binding, target, store)
    from cron.jobs import use_cron_store
    with use_cron_store(store, bookkeeping=True):
        from cron.executions import execution_owner_verified
        if not (store / 'cron' / 'executions.db').is_file() or not execution_owner_verified(binding['execution_id'], job_id):
            raise ValueError("cron store binding no longer owns a verified execution")
        yield



@contextlib.contextmanager
def use_cron_store(home: str | Path, *, bookkeeping: bool = False):
    """Route cron storage to ``home`` without mutating process globals."""
    from cron.jobs import _cron_store_override, _CronStorePaths
    token = _cron_store_override.set(
        _CronStorePaths.for_dir(Path(home).expanduser().resolve() / "cron"))
    bookkeeping_token = _BOOKKEEPING_HOME.set(Path(home).expanduser().resolve()) if bookkeeping else None
    try:
        yield
    finally:
        if bookkeeping_token is not None:
            _BOOKKEEPING_HOME.reset(bookkeeping_token)
        _cron_store_override.reset(token)


def get_cron_store_home(fallback_home: Optional[Path] = None) -> Path:
    """Explicit bookkeeping scope wins; otherwise preserve each consumer's current-home seam."""
    from cron.jobs import _cron_store_override, _current_cron_store
    if fallback_home is not None:
        return _BOOKKEEPING_HOME.get() or Path(fallback_home).resolve()
    override = _cron_store_override.get()
    if override is not None:
        return override.cron_dir.parent
    return _current_cron_store().cron_dir.parent




def get_cron_output_dir() -> Path:
    """Return the output directory for the active cron store context."""
    from cron.jobs import _current_cron_store
    return _current_cron_store().output_dir
