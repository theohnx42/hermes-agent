"""Public-session rejection must stop transport recovery before any screen operation."""

import concurrent.futures
import threading
from unittest.mock import Mock

import pytest
from anyio import ClosedResourceError

from tools.computer_use.cua_backend_session import _CuaDriverSession


def make_session(effects):
    session = _CuaDriverSession.__new__(_CuaDriverSession)
    session._bridge = Mock()
    session._bridge.run.side_effect = effects
    session._lock = threading.Lock()
    session._started = True
    session._declared_session_id = "owned-public-label"
    session._call_tool_async = lambda name, args: (name, args)
    session._stop_lifecycle_locked = Mock()
    session._start_lifecycle_locked = Mock()
    session._transport_reset_callback = Mock()
    return session


REFUSED = {"isError": True, "data": "session is not available to this transport"}


@pytest.mark.parametrize("failure,operation", [
    (concurrent.futures.TimeoutError(), "list_windows"),
    (ClosedResourceError(), "list_windows"),
    (ClosedResourceError(), "type_text"),
])
def test_rejected_rebind_never_dispatches_or_replays_operation(failure, operation):
    session = make_session([failure, REFUSED])
    if isinstance(failure, concurrent.futures.TimeoutError):
        unknown = session.call_tool("type_text", {"text": "possibly delivered"})
        assert unknown["structuredContent"]["code"] == "timeout_outcome_unknown"
        result = session.call_tool(operation, {})
    else:
        result = session.call_tool(operation, {})
    expected = "transport_outcome_unknown" if operation == "type_text" else "session_rebind_refused"
    assert result["structuredContent"]["code"] == expected
    assert session._timeout_suspect is True
    session._transport_reset_callback.assert_called()
    calls = session._bridge.run.call_args_list
    assert len(calls) == 2
    assert calls[-1].args[0] == ("start_session", {"session": "owned-public-label"})
    # A rejected context stays closed rather than looping on new transports or inputs.
    assert session.call_tool("type_text", {"text": "do not send"})["structuredContent"]["code"] == "session_rebind_refused"
    assert session._bridge.run.call_args_list == calls


def test_explicit_fresh_public_context_recovers_only_after_native_success():
    session = make_session([REFUSED, REFUSED, {"isError": False}, {"isError": False}])
    session._timeout_suspect = True
    assert session.call_tool("list_windows", {})["isError"] is True
    assert session.call_tool("start_session", {"session": "fresh-public-label"})["isError"] is True
    assert session.call_tool("list_windows", {})["isError"] is True
    assert session._bridge.run.call_count == 2
    assert session.call_tool("start_session", {"session": "fresh-public-label"})["isError"] is False
    assert session._timeout_suspect is False
    assert session.call_tool("list_windows", {})["isError"] is False
    assert [call.args[0][0] for call in session._bridge.run.call_args_list] == [
        "start_session", "start_session", "start_session", "list_windows",
    ]
