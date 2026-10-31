"""Terminal observers keep the inbound transport identity without changing task ownership."""
from unittest.mock import patch
import logging
import sys
from types import SimpleNamespace

import pytest

from hermes_cli.plugins_dispatch import PluginDispatchMixin
from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent, _run


@pytest.mark.parametrize("message_id", [None, "bot:actual-delivery"])
def test_terminal_hook_keeps_observed_transport_id_and_legacy_callback_compat(message_id):
    agent = _StubAgent(raise_in=())
    if message_id is not None:
        agent._persist_user_message_platform_id = message_id
    observed = {}
    legacy = []

    def old_callback(session_id, completed):
        legacy.append((session_id, completed))

    def new_callback(**kwargs):
        observed.update(kwargs)

    def dispatch(name, _logger, **kwargs):
        if name == "on_session_end":
            PluginDispatchMixin._invoke_hook_callback(old_callback, kwargs)
            PluginDispatchMixin._invoke_hook_callback(new_callback, kwargs)
        return []

    with patch.dict(sys.modules, {"agent.conversation_loop": SimpleNamespace(logger=logging.getLogger("test"))}), \
         patch("agent.turn_finalizer._invoke_hook_safely", side_effect=dispatch):
        result = _run(agent, final_response="done", api_call_count=1,
                      turn_exit_reason="text_response(stop)")

    assert result["completed"] is True
    assert observed["platform_message_id"] == message_id
    assert observed["task_id"] == "task-1"
    assert observed["turn_id"] == "turn-1"
    assert legacy == [("sess-1", True)]
