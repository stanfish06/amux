"""The `-a` spec grammar: `AGENT[@MODEL][/EFFORT][:COUNT]`.

Two properties carry the whole feature. Model and effort are split only for
agents amux launches, so a raw command containing `@` or `/` survives whole;
and values are never checked against a list amux maintains, so a model shipped
after this release still works.
"""

from __future__ import annotations

import pytest

from amux import core
from amux.shared import AgentRequest, render_tuning


def parse(*specs: str, rows=None, cols=None) -> list[AgentRequest]:
    return core.parse_agent_specs(list(specs), rows, cols)


# --- the grammar, one subset at a time -------------------------------------


def test_a_bare_agent_carries_no_tuning():
    assert parse("claude") == [AgentRequest("claude")]


def test_model_only():
    assert parse("codex@gpt-5.6-sol") == [AgentRequest("codex", model="gpt-5.6-sol")]


def test_effort_only():
    assert parse("claude/high") == [AgentRequest("claude", effort="high")]


def test_model_and_effort():
    assert parse("claude@opus/high") == [
        AgentRequest("claude", model="opus", effort="high")
    ]


def test_model_effort_and_count():
    assert parse("claude@opus/high:2") == [
        AgentRequest("claude", model="opus", effort="high")
    ] * 2


def test_count_alone_is_unchanged():
    assert parse("claude:3") == [AgentRequest("claude")] * 3


def test_no_specs_at_all_still_means_one_claude():
    assert parse() == [AgentRequest("claude")]


def test_values_are_not_validated():
    """A model or effort amux has never heard of is forwarded, not refused."""
    assert parse("claude@some-new-model/some-new-level") == [
        AgentRequest("claude", model="some-new-model", effort="some-new-level")
    ]


# --- raw commands are never split ------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "/usr/bin/bash",
        "ssh user@host",
        "echo hello",
        "bash -c 'x/y@z'",
        "my-agent@thing",
    ],
)
def test_raw_commands_pass_through_whole(spec):
    assert parse(spec) == [AgentRequest(spec)]


def test_a_raw_command_keeps_its_count():
    assert parse("bash:2") == [AgentRequest("bash")] * 2


def test_a_raw_command_gets_no_tuning_flags():
    """Nothing to render for an agent with no entry in the flag table."""
    assert render_tuning(AgentRequest("/usr/bin/bash")) == ()


# --- mixed grids ------------------------------------------------------------


def test_a_countless_spec_absorbs_the_remainder():
    assert parse("claude@opus/high:3", "codex@gpt-5.6-sol", rows=2, cols=2) == [
        AgentRequest("claude", model="opus", effort="high")
    ] * 3 + [AgentRequest("codex", model="gpt-5.6-sol")]


def test_a_mixed_grid_keeps_each_spec_own_tuning():
    assert parse("claude@opus/high", "codex/xhigh", "bash") == [
        AgentRequest("claude", model="opus", effort="high"),
        AgentRequest("codex", effort="xhigh"),
        AgentRequest("bash"),
    ]


def test_two_countless_specs_with_a_known_shape_are_still_refused():
    with pytest.raises(ValueError, match="at most one agent spec"):
        parse("claude@opus", "codex/high", rows=2, cols=2)


def test_no_panes_left_names_the_agent_not_the_whole_spec():
    with pytest.raises(ValueError, match="no panes left for 'codex'"):
        parse("claude:4", "codex@gpt-5.6-sol", rows=2, cols=2)


# --- structural errors ------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("claude@", "empty model"),
        ("claude@/high", "empty model"),
        ("claude/", "empty effort"),
        ("claude@opus/", "empty effort"),
        ("", "empty agent spec"),
        (":2", "malformed agent spec"),
    ],
)
def test_malformed_specs_are_rejected(spec, message):
    with pytest.raises(ValueError, match=message):
        parse(spec)


def test_the_invalid_count_error_points_at_the_escape_hatch():
    """A Bedrock-style id ending `:0` lands here, so the way out must be here."""
    with pytest.raises(ValueError, match="raw command"):
        parse("claude@anthropic.claude-sonnet-v1:0")


# --- rendering --------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        (AgentRequest("claude"), ()),
        (AgentRequest("claude", model="opus"), ("--model", "opus")),
        (AgentRequest("claude", effort="high"), ("--effort", "high")),
        (
            AgentRequest("claude", model="opus", effort="high"),
            ("--model", "opus", "--effort", "high"),
        ),
        (AgentRequest("codex", model="gpt-5.6-sol"), ("-m", "gpt-5.6-sol")),
        (
            AgentRequest("codex", effort="xhigh"),
            ("-c", "model_reasoning_effort=xhigh"),
        ),
        (
            AgentRequest("codex", model="gpt-5.6-sol", effort="xhigh"),
            ("-m", "gpt-5.6-sol", "-c", "model_reasoning_effort=xhigh"),
        ),
    ],
)
def test_each_cli_gets_its_own_spelling(request_, expected):
    assert render_tuning(request_) == expected
