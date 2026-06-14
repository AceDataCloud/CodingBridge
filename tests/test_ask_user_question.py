"""AskUserQuestion answer plumbing: browser selection -> CLI tool result.

The wizard answers a multiple-choice prompt; the answer must ride the
``permission.resolve`` all the way into the claude tool input so the CLI echoes
it back instead of reporting "The user did not answer the questions."
"""

import pytest

from coding_bridge_agent.permissions import Resolution
from coding_bridge_agent.providers.claude import (
    ClaudeProvider,
    _ask_user_question_answers,
)


def test_normalize_single_select():
    answer = {"answers": {"Pick one": "OAuth"}}
    assert _ask_user_question_answers(answer) == {"Pick one": "OAuth"}


def test_normalize_multi_select_comma_joined():
    answer = {"answers": {"Features": ["search", "compose"]}}
    assert _ask_user_question_answers(answer) == {"Features": "search, compose"}


def test_normalize_drops_blank_and_empty():
    answer = {"answers": {"a": "", "b": [], "c": ["  ", "x"], "d": None}}
    assert _ask_user_question_answers(answer) == {"c": "x"}


def test_normalize_handles_missing_answers_key():
    assert _ask_user_question_answers({}) == {}
    assert _ask_user_question_answers({"answers": "nope"}) == {}


def _provider(resolution: Resolution) -> ClaudeProvider:
    async def fake_ask(_tool, _input, _ctx):
        return resolution

    async def emit(_payload):
        return None

    return ClaudeProvider("s1", emit, fake_ask, settings=_Settings())


class _Settings:
    default_cwd = "."
    default_model = None


async def test_can_use_tool_feeds_answer_into_input():
    provider = _provider(Resolution("allow", {"answers": {"Q1": "A", "Q2": ["x", "y"]}}))
    result = await provider._can_use_tool(
        "AskUserQuestion", {"questions": [{"question": "Q1"}], "metadata": {"k": "v"}}, None
    )
    assert result.behavior == "allow"
    # The user's selection is merged in as `answers`; the original questions and
    # metadata are preserved so the CLI's own `call` can build the result.
    assert result.updated_input["answers"] == {"Q1": "A", "Q2": "x, y"}
    assert result.updated_input["questions"] == [{"question": "Q1"}]
    assert result.updated_input["metadata"] == {"k": "v"}


async def test_can_use_tool_passes_freeform_response():
    provider = _provider(
        Resolution("allow", {"answers": {"Q1": "A"}, "response": "  extra context  "})
    )
    result = await provider._can_use_tool("AskUserQuestion", {"questions": []}, None)
    assert result.updated_input["response"] == "  extra context  "


async def test_can_use_tool_allow_without_answer_is_plain_allow():
    # Allowed but no structured answer (e.g. resolved before the wizard wired it):
    # fall back to a bare allow rather than injecting an empty answer.
    provider = _provider(Resolution("allow", None))
    result = await provider._can_use_tool("AskUserQuestion", {"questions": []}, None)
    assert result.behavior == "allow"
    assert result.updated_input is None


async def test_can_use_tool_empty_answers_is_plain_allow():
    provider = _provider(Resolution("allow", {"answers": {}}))
    result = await provider._can_use_tool("AskUserQuestion", {"questions": []}, None)
    assert result.updated_input is None


async def test_can_use_tool_ordinary_tool_ignores_answer():
    # A non-AskUserQuestion tool never carries an answer; a plain allow suffices.
    provider = _provider(Resolution("allow", {"answers": {"Q1": "A"}}))
    result = await provider._can_use_tool("Bash", {"command": "ls"}, None)
    assert result.behavior == "allow"
    assert result.updated_input is None


async def test_can_use_tool_deny():
    provider = _provider(Resolution("deny"))
    result = await provider._can_use_tool("AskUserQuestion", {"questions": []}, None)
    assert result.behavior == "deny"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
