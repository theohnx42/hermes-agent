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


def test_noninteractive_entry_expands_before_dispatch(monkeypatch):
    import sys
    from types import SimpleNamespace
    import pytest
    from hermes_cli import cli_single_query as single

    class StopBeforeProvider(Exception):
        pass

    observed = []
    def expand(query, **kwargs):
        observed.append((query, kwargs))
        return "expanded prompt"
    monkeypatch.setattr(single, "_expand_single_query_command", expand)
    names = (
        "_SeededQueryMessage", "_collect_kanban_task_images", "_collect_query_images",
        "_configure_quiet_agent", "_finalize_single_query", "_route_single_query_images",
        "_run_kanban_goal_loop_chat", "_run_quiet_single_query", "_single_query_exit_code",
    )
    fake_cli = SimpleNamespace(**{name: lambda *args, **kwargs: None for name in names})
    fake_cli._should_seed_interactive = lambda *args: False
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setattr("hermes_cli.quiet_single_query.exit_single_query", lambda rc: (_ for _ in ()).throw(StopBeforeProvider()))
    cli = SimpleNamespace(conversation_history=[{"role": "user", "content": "prior"}],
                          _claim_active_session=lambda *args, **kwargs: False)
    for quiet, oneshot in ((True, False), (False, True)):
        with pytest.raises(StopBeforeProvider):
            single._run_single_query_mode(cli, "/learn", None, quiet, oneshot)
    assert observed == [("/learn", {"has_history": True})] * 2
