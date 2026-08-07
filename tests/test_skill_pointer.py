"""Activating the skill: the pointer amux puts in a `claude` system prompt.

Installing the document only makes it *present*. Both agents load skills lazily
by description matching, so a document nobody opens is worth what no document is
worth -- which is the failure the amux skill's own "Common mistakes" section is a
list of. `claude` takes a pointer through `--append-system-prompt`; `codex` has
no such flag and is activated by a bootstrap message instead (group 5).

Two seams compose the pointer, and both are shared with the
`choose-agent-model-and-effort` change: `runtime.HostRuntime.prepare` renders a
`send-keys` string, `sandbox.attach_argv` builds an `sbx run` argument list. They
flatten through one helper, `shared.render_command`, so appending arguments
composes rather than replaces.
"""

from __future__ import annotations

import shlex

from amux import runtime, sandbox, shared


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


def host_command(agent: str, cwd) -> str:
    (launch,) = runtime.HostRuntime().prepare(
        specs(("%1", agent, "alpha")), workspace="ws", task="t0", cwd=str(cwd)
    )
    return launch.keys[-1]


# --- the pointer itself ------------------------------------------------------


def test_the_pointer_says_where_the_agent_is_and_what_governs_it():
    text = shared.SKILL_POINTER
    assert "amux pane" in text
    for topic in ("coordination", "spawning", "messaging", "worktrees", "state"):
        assert topic in text.lower(), topic


def test_the_pointer_names_the_skill_so_it_can_be_invoked_directly():
    assert "`amux`" in shared.SKILL_POINTER


def test_the_pointer_is_a_pointer_and_not_the_document():
    """Short enough not to displace the thing it points at: one paragraph, and a
    small fraction of the ~500-line document it stands in for."""
    document = (
        __import__("amux.sandbox_bootstrap", fromlist=["x"]).skill_source().read_text()
    )
    assert len(shared.SKILL_POINTER) < 600
    assert "\n" not in shared.SKILL_POINTER
    assert len(shared.SKILL_POINTER) < len(document) / 10


def test_only_claude_takes_the_pointer_as_a_launch_argument():
    assert shared.skill_pointer_args("claude") == (
        "--append-system-prompt",
        shared.SKILL_POINTER,
    )
    # codex has no system-prompt append of any kind, so it gets a message.
    assert shared.skill_pointer_args("codex") == ()
    for raw in ("bash", "echo hello", "claude --resume", ""):
        assert shared.skill_pointer_args(raw) == ()


# --- the render-and-quote helper ---------------------------------------------


def test_render_command_appends_rather_than_replaces():
    assert shared.render_command("claude --flag", ["--model", "opus"]) == (
        "claude --flag --model opus"
    )


def test_render_command_leaves_a_bare_command_untouched():
    assert shared.render_command("bash") == "bash"
    assert shared.render_command("bash", []) == "bash"


def test_render_command_makes_each_argument_exactly_one_word():
    rendered = shared.render_command("claude", ["--p", "two words; rm -rf /"])
    assert shlex.split(rendered) == ["claude", "--p", "two words; rm -rf /"]


# --- seam 1: the host launch command -----------------------------------------


def test_a_host_claude_launch_carries_the_pointer_after_its_existing_flags(git_repo):
    command = host_command("claude", git_repo)

    assert command.startswith(runtime.AGENT_COMMANDS["claude"])
    assert shlex.split(command)[-2:] == ["--append-system-prompt", shared.SKILL_POINTER]


def test_the_host_pointer_arrives_as_a_single_argument(git_repo):
    """Unquoted, the pointer would reach `claude` as forty separate words."""
    parts = shlex.split(host_command("claude", git_repo))

    assert parts.count(shared.SKILL_POINTER) == 1
    assert len(parts) == len(shlex.split(runtime.AGENT_COMMANDS["claude"])) + 2


def test_a_host_codex_launch_is_unchanged(git_repo):
    assert host_command("codex", git_repo) == runtime.AGENT_COMMANDS["codex"]


def test_a_host_raw_command_is_launched_verbatim(git_repo):
    """amux does not know an arbitrary command's flags and must not invent one."""
    assert host_command("echo hello", git_repo) == "echo hello"


def test_an_empty_agent_still_sends_nothing(git_repo):
    (launch,) = runtime.HostRuntime().prepare(
        specs(("%1", "", "alpha")), workspace="ws", task="t0", cwd=str(git_repo)
    )
    assert launch.keys == (f"cd {launch.cwd}",)


# --- seam 2: the sandbox attach command --------------------------------------


def test_a_sandboxed_claude_passes_the_pointer_through_to_the_agent():
    assert sandbox.attach_argv("sb1", "claude") == (
        "run",
        "--name",
        "sb1",
        "claude",
        "--",
        "--append-system-prompt",
        shared.SKILL_POINTER,
    )


def test_the_sandbox_pointer_survives_flattening_as_one_argument():
    parts = shlex.split(sandbox.attach_command("sb1", "claude"))

    assert parts.count(shared.SKILL_POINTER) == 1
    assert parts[:5] == ["sbx", "run", "--name", "sb1", "claude"]


def test_a_sandboxed_codex_keeps_its_hook_trust_flag_and_gains_no_pointer():
    """The two seams compose: the hook-trust flag is the sandbox's requirement,
    the pointer is amux's, and neither may quietly displace the other."""
    argv = sandbox.attach_argv("sb1", "codex")

    assert sandbox.HOOK_TRUST_FLAG in argv
    assert shared.SKILL_POINTER not in argv


def test_a_sandbox_attach_with_no_agent_carries_nothing():
    assert sandbox.attach_argv("sb1") == ("run", "--name", "sb1")


def test_both_seams_carry_the_same_pointer(git_repo):
    """One constant, two mechanisms. A pointer that drifted between runtimes
    would make a sandboxed agent and a host agent believe different things."""
    host = shlex.split(host_command("claude", git_repo))
    sandboxed = list(sandbox.attach_argv("sb1", "claude"))

    assert host[-2:] == sandboxed[-2:]
