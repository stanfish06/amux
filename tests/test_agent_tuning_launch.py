"""Model and effort reaching the two places a launch command is composed.

`HostRuntime.prepare` builds shell text that `send-keys` types into a pane, and
`sandbox.attach_argv` builds the argv `sbx run` gets. Both compose the tuning
flags onto whatever the agent already needed, and both have to survive a value
amux never validated.
"""

from __future__ import annotations

import shlex

import pytest

from amux import runtime, sandbox
from amux.shared import AgentRequest


def prepare_one(agent: str, model: str = "", effort: str = "", *, cwd: str) -> tuple:
    """One host launch, outside a repo so no `cd` is prepended."""
    (launch,) = runtime.HostRuntime().prepare(
        [runtime.PaneSpec("%1", agent, "alpha", model, effort)],
        workspace="ws",
        task="t0",
        cwd=cwd,
    )
    return launch.keys


@pytest.fixture
def plain(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    return str(d)


# --- host runtime -----------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "model", "effort", "expected"),
    [
        ("claude", "", "", ""),
        ("claude", "opus", "", " --model opus"),
        ("claude", "", "high", " --effort high"),
        ("claude", "opus", "high", " --model opus --effort high"),
        ("codex", "", "", ""),
        ("codex", "gpt-5.6-sol", "", " -m gpt-5.6-sol"),
        ("codex", "", "xhigh", " -c model_reasoning_effort=xhigh"),
        (
            "codex",
            "gpt-5.6-sol",
            "xhigh",
            " -m gpt-5.6-sol -c model_reasoning_effort=xhigh",
        ),
    ],
)
def test_tuning_is_appended_to_the_agent_command(agent, model, effort, expected, plain):
    """The existing command is kept whole and the flags land after it."""
    assert prepare_one(agent, model, effort, cwd=plain) == (
        runtime.AGENT_COMMANDS[agent] + expected,
    )


def test_an_untuned_spec_launches_todays_exact_command(plain):
    for agent, command in runtime.AGENT_COMMANDS.items():
        assert prepare_one(agent, cwd=plain) == (command,)


def test_a_raw_command_is_left_alone(plain):
    """Raw specs carry no tuning by construction, so nothing is appended."""
    assert prepare_one("echo hi", cwd=plain) == ("echo hi",)


def test_a_raw_command_carrying_tuning_fails_loudly(plain):
    """Unreachable through the parser, and it must stay that way audibly.

    There is no flag spelling for an arbitrary command, so the only choices are
    to drop the values or to refuse. Dropping them is the thing the spec calls
    out by name — neither runtime may silently ignore a model a user asked for.
    """
    with pytest.raises(ValueError, match="no model flag"):
        prepare_one("echo hi", "opus", "high", cwd=plain)


@pytest.mark.parametrize(
    "hostile",
    ["opus; rm -rf /", "$(id)", "`id`", "a b", "x'y", 'x"y', "a|b", "a&b", "a\nb"],
)
def test_hostile_values_arrive_as_one_shell_word(hostile, plain):
    """Values are unvalidated, so the pane's shell must never interpret them."""
    (keys,) = prepare_one("claude", hostile, cwd=plain)
    assert keys.startswith(runtime.AGENT_COMMANDS["claude"] + " ")
    assert shlex.split(keys)[-2:] == ["--model", hostile]


def test_a_hostile_effort_is_quoted_inside_codexs_config_override(plain):
    """codex folds effort into `-c key=value`; the whole pair is one word."""
    (keys,) = prepare_one("codex", "", "high; id", cwd=plain)
    assert shlex.split(keys)[-1] == "model_reasoning_effort=high; id"


# --- docker-sandbox attach --------------------------------------------------


def test_sandboxed_claude_carries_its_flags():
    """claude has no AGENT_ATTACH_ARGS entry, so the `--` form must still appear."""
    assert sandbox.attach_argv(
        "sb1", "claude", AgentRequest("claude", "opus", "high")
    ) == ("run", "--name", "sb1", "claude", "--", "--model", "opus", "--effort", "high")


def test_sandboxed_codex_composes_with_the_hook_trust_flag():
    argv = sandbox.attach_argv(
        "sb1", "codex", AgentRequest("codex", "gpt-5.6-sol", "xhigh")
    )
    assert argv == (
        "run",
        "--name",
        "sb1",
        "codex",
        "--",
        sandbox.HOOK_TRUST_FLAG,
        "-m",
        "gpt-5.6-sol",
        "-c",
        "model_reasoning_effort=xhigh",
    )
    # The runtime's own flag stays first: tuning composes onto it, never over it.
    assert argv.index(sandbox.HOOK_TRUST_FLAG) < argv.index("-m")


def test_an_untuned_attach_is_unchanged():
    for agent in ("claude", "codex"):
        assert sandbox.attach_argv("sb1", agent) == sandbox.attach_argv(
            "sb1", agent, AgentRequest(agent)
        )
    assert sandbox.attach_command("sb1", "claude") == "sbx run --name sb1"


def test_the_attach_command_quotes_a_hostile_value():
    command = sandbox.attach_command(
        "sb1", "claude", AgentRequest("claude", "opus; id")
    )
    assert shlex.split(command)[-1] == "opus; id"


def test_both_runtimes_render_the_same_flags(plain):
    """Same spec, same flags, whichever runtime prepares the pane."""
    request = AgentRequest("codex", "gpt-5.6-sol", "xhigh")
    (keys,) = prepare_one(request.agent, request.model, request.effort, cwd=plain)
    base = len(shlex.split(runtime.AGENT_COMMANDS["codex"]))
    host_flags = shlex.split(keys)[base:]

    sandboxed = list(sandbox.attach_argv("sb1", request.agent, request))
    tail = sandboxed[sandboxed.index("--") + 1 :]

    assert [a for a in tail if a != sandbox.HOOK_TRUST_FLAG] == host_flags
