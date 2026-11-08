"""Ollama tool-capability contract for a completion-only local definition.

Simulated coverage only. Nothing in this file loads condor weights or talks
to a live Ollama server. A fixture shaped like a model ``tool_calls`` payload
is parsed by the real Hermes executor. That is not live 14B inference.

The settled local tag in these tests is ``condor-liberated:14b``. The 7B
canary appears only as a fallback that must not be activated.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


COMPLETION_ONLY_SHOW = {
    "capabilities": ["completion"],
    "template": "{{ if .System }}{{ .System }}{{ end }}{{ .Prompt }}{{ .Response }}",
    "parameters": "stop USER\nstop ASSISTANT",
}

TOOLS_ADVERTISED_SHOW = {
    "capabilities": ["completion", "tools"],
    # The Go template may still look like a completion template when a parser
    # or the GGUF chat template is what Ollama used to advertise tools.
    # Hermes must follow the capabilities list, not invent one from a copied
    # ``.Tools`` stanza.
    "template": "{{ .System }}{{ .Prompt }}{{ .Response }}",
    "parameters": "",
}

READ_ONLY_DECLARATION = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "read-only canary declaration",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

SENTINEL = "CONDOR14B_READONLY_SENTINEL"
MODEL = "condor-liberated:14b"
SEVEN_B = "phoenix-redteam-abliterated:7b-hermes-deterministic"


class _Api400(Exception):
    """Shape of Ollama's live HTTP 400, without using Hermes's preflight type."""

    def __init__(self, message: str):
        super().__init__(message)
        self.status_code = 400
        self.body = {"error": {"message": message, "type": "api_error"}}


def _mock_tool_call(name, arguments, call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content, finish_reason="stop", tool_calls=None):
    return SimpleNamespace(
        id="chatcmpl-sim",
        model=MODEL,
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _request_payload(call):
    return call.kwargs


@pytest.fixture()
def loop_agent():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="http://127.0.0.1:11434/v1",
            provider="custom",
            model=MODEL,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent.tools = [READ_ONLY_DECLARATION]
        # The session's sanctioned set is the read-only declaration only.
        # Init saw an empty tool catalog, so this has to be set explicitly.
        agent.valid_tool_names = {"read_file"}
        agent.model = MODEL
        agent.provider = "custom"
        agent.base_url = "http://127.0.0.1:11434/v1"
        agent._fallback_chain = [
            {
                "provider": "custom",
                "model": SEVEN_B,
                "base_url": "http://127.0.0.1:11434/v1",
            }
        ]
        agent._fallback_index = 0
        agent._fallback_activated = False
        return agent


def _run(agent, prompt="plan the next read-only step"):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(prompt)


class TestShowAdvertisement:
    def test_completion_only_capabilities_are_not_tools(self):
        from agent.model_metadata import ollama_show_advertises_tools

        assert ollama_show_advertises_tools(COMPLETION_ONLY_SHOW) is False

    def test_capabilities_list_wins_over_a_completion_template(self):
        from agent.model_metadata import ollama_show_advertises_tools

        assert ollama_show_advertises_tools(TOOLS_ADVERTISED_SHOW) is True

    def test_tools_token_in_a_go_template_is_not_an_advertisement(self):
        """Copying ``.Tools`` into a template does not make the weights capable."""
        from agent.model_metadata import ollama_show_advertises_tools

        assert (
            ollama_show_advertises_tools(
                {
                    "template": "{{ .System }}{{ .Prompt }}{{ .Response }}{{ .Tools }}",
                    "parameters": "stop USER",
                }
            )
            is None
        )

    def test_empty_capabilities_are_unknown(self):
        from agent.model_metadata import ollama_show_advertises_tools

        assert ollama_show_advertises_tools({"capabilities": []}) is None
        assert ollama_show_advertises_tools({}) is None
        assert ollama_show_advertises_tools(None) is None


class TestQueryOllamaSupportsTools:
    def test_posts_show_and_reads_capabilities(self):
        from agent.model_metadata import query_ollama_supports_tools
        from tests.test_ollama_num_ctx import _mock_httpx_client

        mock_ctx, mock_client = _mock_httpx_client(COMPLETION_ONLY_SHOW)
        with patch("agent.model_metadata.detect_local_server_type", return_value="ollama"):
            import httpx

            with patch.object(httpx, "Client", return_value=mock_ctx):
                result = query_ollama_supports_tools(
                    MODEL, "http://127.0.0.1:11434/v1"
                )

        assert result is False
        posted = mock_client.post.call_args
        assert posted[0][0].endswith("/api/show")
        assert posted[1]["json"]["name"] == MODEL

    def test_non_ollama_server_is_unknown_and_does_not_block(self):
        from agent.model_metadata import query_ollama_supports_tools

        with patch("agent.model_metadata.detect_local_server_type", return_value="vllm"):
            assert (
                query_ollama_supports_tools("llama", "http://127.0.0.1:8000/v1")
                is None
            )


class TestClassification:
    def test_live_400_does_not_retry_compress_or_fall_back(self):
        from agent.error_classifier import FailoverReason, classify_api_error

        result = classify_api_error(
            _Api400(f"{MODEL} does not support tools"),
            provider="custom",
            model=MODEL,
            approx_tokens=180000,
            context_length=8192,
            num_messages=120,
        )
        assert result.reason == FailoverReason.ollama_tools_unsupported
        assert result.retryable is False
        assert result.should_fallback is False
        assert result.should_compress is False
        assert result.should_rotate_credential is False

    def test_openrouter_tool_endpoint_miss_stays_model_not_found(self):
        from agent.error_classifier import FailoverReason, classify_api_error

        class _Missing(Exception):
            def __init__(self):
                super().__init__("No endpoints found that support tool use")
                self.status_code = 404
                self.body = {
                    "error": {"message": "No endpoints found that support tool use"}
                }

        result = classify_api_error(
            _Missing(), provider="openrouter", model="some/model"
        )
        assert result.reason == FailoverReason.model_not_found
        assert result.reason != FailoverReason.ollama_tools_unsupported

    def test_user_message_names_the_tag_and_refuses_substitution(self):
        from agent.error_classifier import ollama_tools_unsupported_message

        text = ollama_tools_unsupported_message(MODEL)
        assert MODEL in text
        assert "did not drop" in text
        assert "did not invent" in text
        assert "7B" in text or "7b" in text.lower()


class TestTransportGuard:
    def test_unadvertised_model_raises_before_a_stripped_request(self):
        from agent.error_classifier import OllamaToolsUnsupported
        from agent.transports.chat_completions import ChatCompletionsTransport
        from providers import get_provider_profile

        transport = ChatCompletionsTransport()
        with patch(
            "agent.model_metadata.query_ollama_supports_tools", return_value=False
        ):
            with pytest.raises(OllamaToolsUnsupported) as caught:
                transport.build_kwargs(
                    model=MODEL,
                    messages=[{"role": "user", "content": "plan"}],
                    tools=[READ_ONLY_DECLARATION],
                    base_url="http://127.0.0.1:11434/v1",
                    provider_profile=get_provider_profile("custom"),
                )
        assert "does not support tools" in str(caught.value)
        assert caught.value.status_code == 400

    def test_advertised_model_keeps_the_declaration(self):
        from agent.transports.chat_completions import ChatCompletionsTransport
        from providers import get_provider_profile

        transport = ChatCompletionsTransport()
        with patch(
            "agent.model_metadata.query_ollama_supports_tools", return_value=True
        ):
            kwargs = transport.build_kwargs(
                model=MODEL,
                messages=[{"role": "user", "content": "plan"}],
                tools=[READ_ONLY_DECLARATION],
                base_url="http://127.0.0.1:11434/v1",
                provider_profile=get_provider_profile("custom"),
            )
        assert kwargs["model"] == MODEL
        assert kwargs["tools"] == [READ_ONLY_DECLARATION]
        assert kwargs["tools"][0]["function"]["description"] == (
            "read-only canary declaration"
        )

    def test_unknown_advertisement_still_sends_tools(self):
        from agent.transports.chat_completions import ChatCompletionsTransport

        transport = ChatCompletionsTransport()
        with patch(
            "agent.model_metadata.query_ollama_supports_tools", return_value=None
        ):
            kwargs = transport.build_kwargs(
                model=MODEL,
                messages=[{"role": "user", "content": "plan"}],
                tools=[READ_ONLY_DECLARATION],
                base_url="http://127.0.0.1:11434/v1",
            )
        assert kwargs["tools"] == [READ_ONLY_DECLARATION]

    def test_plain_turn_does_not_probe(self):
        from agent.transports.chat_completions import ChatCompletionsTransport

        transport = ChatCompletionsTransport()
        with patch(
            "agent.model_metadata.query_ollama_supports_tools"
        ) as probe:
            transport.build_kwargs(
                model=MODEL,
                messages=[{"role": "user", "content": "hello"}],
                tools=None,
                base_url="http://127.0.0.1:11434/v1",
            )
        probe.assert_not_called()


class TestConversationRefusal:
    def test_live_400_keeps_14b_and_does_not_call_the_7b_fallback(self, loop_agent):
        loop_agent.client.chat.completions.create.side_effect = _Api400(
            f"{MODEL} does not support tools"
        )
        with patch(
            "agent.model_metadata.query_ollama_supports_tools", return_value=None
        ):
            result = _run(loop_agent)

        assert result["failed"] is True
        assert result.get("failure_reason") == "ollama_tools_unsupported"
        assert loop_agent.model == MODEL
        assert loop_agent._fallback_activated is False
        assert loop_agent._fallback_index == 0
        assert SEVEN_B not in (result.get("final_response") or "")
        sent = _request_payload(loop_agent.client.chat.completions.create.call_args)
        assert sent["model"] == MODEL
        assert sent["tools"][0]["function"]["name"] == "read_file"
        assert "read-only canary declaration" in sent["tools"][0]["function"]["description"]
        assert "did not drop" in result["final_response"]

    def test_preflight_refusal_does_not_send_the_chat_request(self, loop_agent):
        loop_agent.client.chat.completions.create.side_effect = AssertionError(
            "chat request must not be sent when tools are unadvertised"
        )
        with patch(
            "agent.model_metadata.query_ollama_supports_tools", return_value=False
        ):
            result = _run(loop_agent)

        assert loop_agent.client.chat.completions.create.call_count == 0
        assert result["failed"] is True
        assert result.get("failure_reason") == "ollama_tools_unsupported"
        assert loop_agent.model == MODEL
        assert loop_agent._fallback_activated is False


class TestSimulatedToolRoundTrip:
    """Real executor, fixture model output. Not a live 14B generation."""

    def _turn(self, loop_agent, tool_calls, followup, **patches):
        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response("", finish_reason="tool_calls", tool_calls=tool_calls),
            _mock_response(followup, finish_reason="stop"),
        ]
        stack = [
            patch(
                "agent.model_metadata.query_ollama_supports_tools", return_value=True
            )
        ]
        for target, value in patches.items():
            stack.append(patch(target, value))
        with stack[0]:
            if len(stack) == 1:
                return _run(loop_agent)
            with stack[1]:
                return _run(loop_agent)

    def test_read_only_result_is_fed_back_to_the_same_model(self, loop_agent, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text(SENTINEL + "\n", encoding="utf-8")
        call = _mock_tool_call(
            "read_file",
            json.dumps({"path": str(target)}),
            "call_read_ok",
        )
        followup = f"simulated follow-up saw {SENTINEL}"
        result = self._turn(loop_agent, [call], followup)

        assert result.get("failed") is not True
        assert loop_agent.model == MODEL
        assert loop_agent.client.chat.completions.create.call_count == 2
        first = _request_payload(
            loop_agent.client.chat.completions.create.call_args_list[0]
        )
        second = _request_payload(
            loop_agent.client.chat.completions.create.call_args_list[1]
        )
        assert first["model"] == MODEL
        assert first["tools"][0]["function"]["description"] == (
            "read-only canary declaration"
        )
        tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
        assert tool_msgs
        assert SENTINEL in tool_msgs[-1]["content"]
        assert tool_msgs[-1]["tool_call_id"] == "call_read_ok"
        assert result["final_response"] == followup
        assert second["model"] == MODEL

    def test_invalid_arguments_are_not_executed(self, loop_agent, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text(SENTINEL + "\n", encoding="utf-8")
        # Cut off before a closing brace. Hermes treats that as truncated
        # tool-call JSON and refuses the call instead of executing read_file
        # or asking the model to continue as if the read succeeded.
        call = _mock_tool_call("read_file", "{not-json", "call_bad_args")
        result = self._turn(loop_agent, [call], "must not be requested")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result.get("completed") is False
        assert "truncat" in (result.get("final_response") or "").lower()
        assert SENTINEL not in json.dumps(result.get("messages") or [])
        assert result.get("final_response") != "must not be requested"
        assert loop_agent.model == MODEL

    def test_tool_error_is_returned_not_rewritten_as_success(self, loop_agent, tmp_path):
        missing = tmp_path / "missing-condor.txt"
        call = _mock_tool_call(
            "read_file",
            json.dumps({"path": str(missing)}),
            "call_missing",
        )
        result = self._turn(loop_agent, [call], "simulated follow-up after tool error")
        second = _request_payload(
            loop_agent.client.chat.completions.create.call_args_list[1]
        )
        tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
        body = tool_msgs[-1]["content"]
        parsed = json.loads(body)
        assert "error" in parsed
        assert SENTINEL not in body
        assert result["final_response"] == "simulated follow-up after tool error"

    def test_denied_approval_does_not_return_file_contents(self, loop_agent, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text(SENTINEL + "\n", encoding="utf-8")
        call = _mock_tool_call(
            "read_file",
            json.dumps({"path": str(target)}),
            "call_denied",
        )

        def _hook(name, **kwargs):
            if name == "pre_tool_call":
                return [{"action": "approve", "message": "read-only canary"}]
            return []

        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response("", finish_reason="tool_calls", tool_calls=[call]),
            _mock_response("simulated follow-up after denial", finish_reason="stop"),
        ]
        with (
            patch(
                "agent.model_metadata.query_ollama_supports_tools", return_value=True
            ),
            patch("hermes_cli.plugins.invoke_hook", _hook),
            patch(
                "tools.approval.request_tool_approval",
                return_value={"approved": False, "message": "denied by canary policy"},
            ),
        ):
            result = _run(loop_agent)

        second = _request_payload(
            loop_agent.client.chat.completions.create.call_args_list[1]
        )
        tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
        body = tool_msgs[-1]["content"]
        assert SENTINEL not in body
        assert "BLOCKED" in body or "denied" in body.lower()
        assert result["final_response"] == "simulated follow-up after denial"
        assert loop_agent.model == MODEL
