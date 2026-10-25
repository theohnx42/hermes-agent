"""Prompt-producing commands must not depend on the absent interactive loop."""
from unittest.mock import patch

from hermes_cli.cli_single_query import _expand_single_query_command


def test_focused_learn_uses_authored_prompt():
    with patch("agent.learn_prompt.build_learn_prompt", return_value="built") as build:
        assert _expand_single_query_command(" /learn project workflow ") == "built"
        build.assert_called_once_with("project workflow")


def test_fresh_bare_learn_does_not_claim_prior_history():
    with patch("agent.learn_prompt.build_learn_prompt") as build:
        result = _expand_single_query_command("/learn")
        assert "no prior workflow" in result
        assert "Do not investigate" in result
        build.assert_not_called()


def test_resumed_bare_learn_preserves_conversation_distillation():
    with patch("agent.learn_prompt.build_learn_prompt", return_value="history prompt") as build:
        assert _expand_single_query_command("/learn", has_history=True) == "history prompt"
        build.assert_called_once_with("")


def test_init_uses_current_prompt_builder():
    with patch("hermes_cli.init_command.build_init_prompt_for_cwd", return_value="init prompt") as build:
        assert _expand_single_query_command("/init service") == "init prompt"
        build.assert_called_once_with(extra="service", session_key="")


def test_ordinary_input_and_command_prefixes_remain_literal():
    for value in ("/learning", "/initialize", "ordinary query", None, [{"type": "text"}]):
        assert _expand_single_query_command(value) is value
