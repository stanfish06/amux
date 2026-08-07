"""Which panes carry a bootstrap payload, and what it says.

`test_bootstrap_send` owns the mechanism -- waiting, sending, verifying. This
owns the decision: `codex` gets a message because it has no system-prompt append
flag, `claude` gets none because its pointer is already in the system prompt and
a message would cost it a turn to be told what it knows, and a raw command gets
none because amux does not speak for a command it does not know.

The path in the message is the part worth checking twice. A sandboxed agent reads
through its own `$HOME` inside a microVM, so a host path there names nothing.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import fake_tmux
from amux import core, runtime, sandbox_bootstrap, shared
from test_bootstrap_send import capture
from test_host_grid_snapshot import tmux_calls  # noqa: F401 - fixture


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


def host_launches(cwd, *panes) -> dict[str, runtime.Launch]:
    prepared = runtime.HostRuntime().prepare(
        specs(*panes), workspace="ws", task="t0", cwd=str(cwd)
    )
    return {launch.pane: launch for launch in prepared}


# --- who gets one -------------------------------------------------------------


def test_a_codex_pane_carries_a_bootstrap_payload(git_repo, isolate_home):
    launches = host_launches(git_repo, ("%1", "codex", "alpha"))

    assert launches["%1"].bootstrap


def test_a_claude_pane_carries_none(git_repo, isolate_home):
    """Its pointer rode the launch command. A message would spend a turn."""
    launches = host_launches(git_repo, ("%1", "claude", "alpha"))

    assert launches["%1"].bootstrap == ""
    assert shared.SKILL_POINTER in launches["%1"].keys[-1]


def test_a_raw_command_carries_none(git_repo, isolate_home):
    launches = host_launches(git_repo, ("%1", "bash", "alpha"))

    assert launches["%1"].bootstrap == ""


def test_the_two_mechanisms_are_mutually_exclusive_by_construction():
    """Not by two lists that could drift apart: `skill_bootstrap_message` refuses
    for exactly the agents `skill_pointer_args` speaks for."""
    for agent in shared.POINTER_FLAGS:
        assert shared.skill_bootstrap_message(agent, "/somewhere/SKILL.md") == ""
        assert shared.skill_pointer_args(agent)
    assert shared.skill_bootstrap_message("codex", "/somewhere/SKILL.md")
    assert shared.skill_pointer_args("codex") == ()


# --- what it says -------------------------------------------------------------


def test_the_host_message_names_the_host_path_that_was_written(
    git_repo, isolate_home
):
    launches = host_launches(git_repo, ("%1", "codex", "alpha"))
    installed = isolate_home / ".codex" / "skills" / "amux" / "SKILL.md"

    assert installed.is_file()
    assert str(installed) in launches["%1"].bootstrap


def test_the_message_is_attributed_to_amux(git_repo, isolate_home):
    """It arrives as raw keystrokes in the agent's own prompt -- no envelope, no
    sender field. Without a prefix the agent cannot tell it from its human."""
    launches = host_launches(git_repo, ("%1", "codex", "alpha"))

    assert launches["%1"].bootstrap.startswith("[amux]")


def test_the_message_carries_the_same_pointer_the_flag_does(git_repo, isolate_home):
    """One constant, two mechanisms. Two texts would be two things to keep true."""
    launches = host_launches(git_repo, ("%1", "codex", "alpha"))

    assert shared.SKILL_POINTER in launches["%1"].bootstrap


def test_an_install_that_failed_sends_nobody_to_read_it(
    git_repo, isolate_home, monkeypatch
):
    """Pointing an agent at a document that is not there spends its first turn
    on a dead end. The install failure is already reported on its own."""
    monkeypatch.setattr(
        runtime.sandbox_bootstrap,
        "install_host_skill",
        lambda agent, **kw: sandbox_bootstrap.HostSkillInstalled(reason="disk on fire"),
    )
    launches = host_launches(git_repo, ("%1", "codex", "alpha"))

    assert launches["%1"].bootstrap == ""


# --- the sandbox runtime names the in-VM path ---------------------------------


def test_a_sandboxed_codex_is_pointed_inside_its_own_vm(sandbox_launch):
    launch = sandbox_launch("codex")

    assert "/home/agent/.codex/skills/amux/SKILL.md" in launch.bootstrap
    assert str(Path.home()) not in launch.bootstrap


def test_a_sandboxed_claude_still_carries_no_message(sandbox_launch):
    assert sandbox_launch("claude").bootstrap == ""


def test_a_shared_skills_sandbox_is_still_pointed_at_its_own_home(sandbox_launch):
    """Under `--share-skills` the bytes arrive from the host directory, but the
    agent still reads them through its own `$HOME`."""
    launch = sandbox_launch("codex", share_skills=True)

    assert "/home/agent/.codex/skills/amux/SKILL.md" in launch.bootstrap


def test_a_failed_in_vm_install_sends_nobody_to_read_it(sandbox_launch, capsys):
    """The sandbox half of the rule the host half already kept.

    This site used to RESOLVE the in-VM path rather than report what landed, so
    it was non-empty whatever happened -- amux warned that the agent had no
    skill and then typed a message telling that agent to go read it. The cost is
    the agent's entire first turn on a file that is not there.
    """
    launch = sandbox_launch("codex", skill_reason="no space left on device")

    assert launch.bootstrap == ""
    assert "no amux skill installed" in capsys.readouterr().out


def test_a_failed_host_install_sends_nobody_to_read_it_under_shared_skills(
    sandbox_launch, capsys
):
    """The same defect by the other route. With `--share-skills` nothing is
    copied into the VM at all, so it is the HOST write that decides whether the
    in-VM path has anything behind it."""
    launch = sandbox_launch("codex", share_skills=True, host_reason="disk on fire")

    assert launch.bootstrap == ""
    assert "no amux skill installed" in capsys.readouterr().out


def test_a_shared_skills_grid_writes_once_per_kind_not_once_per_pane(
    sandbox_prepare,
):
    """`Repeated agents of one kind write once` is not scoped to the host
    runtime. `--share-skills` backs every VM with the same host directory, so
    there is one destination per agent kind however many panes want it."""
    sandbox_prepare("claude", "claude", "codex", "claude", share_skills=True)

    assert sandbox_prepare.host_install_calls == ["claude", "codex"]


def test_a_sandbox_grid_without_shared_skills_writes_no_host_destination(
    sandbox_prepare,
):
    sandbox_prepare("claude", "codex")

    assert sandbox_prepare.host_install_calls == []


# --- one pane's failure is one pane's failure ---------------------------------


def never_ready(monkeypatch) -> None:
    """Make every pane in the grid render a real still-starting `codex`.

    Through the real `interface_ready`, not around it: a stub returning False
    would prove the reporting works and nothing about the thing that decides.
    The timeout goes to zero so the wait is one poll rather than 45 seconds.
    """
    monkeypatch.setattr(
        fake_tmux, "READY_CAPTURE", capture("codex_0.146.0_starting")
    )
    monkeypatch.setattr(core, "BOOTSTRAP_READY_TIMEOUT_S", 0.0)


class NeverReadyRuntime:
    """A host runtime whose codex pane will never show an interface."""

    kind = "never-ready"

    def preflight(self, agents, *, workspace, task, cwd): ...

    def resumable_names(self, *, workspace, task, cwd):
        return {}

    def rollback(self):
        return []

    def prepare(self, panes, *, workspace, task, cwd, socket=""):
        return [
            runtime.Launch(
                pane=spec.pane,
                keys=("the launch command",),
                bootstrap="[amux] read the skill" if spec.agent == "codex" else "",
            )
            for spec in panes
        ]


def test_a_bootstrap_timeout_costs_one_agent_its_pointer_and_nothing_else(
    git_repo, isolate_home, capsys, monkeypatch, tmux_calls
):
    never_ready(monkeypatch)
    window = fake_tmux.new_window()
    random.seed(1234)

    grid = core._build_grid(  # noqa: SLF001
        window, 1, 2, ["claude", "codex"], str(git_repo),
        workspace="ws", task="t0", runtime=NeverReadyRuntime(),
    )

    # Every pane was created, launched and told to report its state.
    assert len(grid.agent_panes) == 2
    launched = [e for e in window.server.log if e[0] == "send_keys"]
    assert [e[2] for e in launched] == ["the launch command"] * 2
    # ...and the one that could not be reached was named, once.
    out = capsys.readouterr().out
    assert out.count("was not given amux's skill pointer") == 1
    assert grid.agent_panes[1].name in out


def test_the_report_names_the_agent_and_leaves_the_pane_running(
    git_repo, isolate_home, capsys, monkeypatch, tmux_calls
):
    never_ready(monkeypatch)
    random.seed(1234)

    grid = core._build_grid(  # noqa: SLF001
        fake_tmux.new_window(), 1, 1, ["codex"], str(git_repo),
        workspace="ws", task="t0", runtime=NeverReadyRuntime(),
    )

    out = capsys.readouterr().out
    assert "not ready" in out
    assert "it is running" in out
    assert grid.agent_panes[0].state == "starting"


@pytest.fixture
def sandbox_prepare(git_repo, fake_sbx, isolate_home, monkeypatch):
    """Prepare a real sandbox grid, with `sbx` and the in-VM work faked out.

    The `install_skill` fake resolves the destination the same way the real one
    does rather than returning a stand-in path. An earlier version returned
    `/home/agent/x` and always succeeded, which is precisely why the suite could
    not see a failed install still sending an agent to read the document.
    """
    from amux import sandbox

    calls: list[str] = []

    def make(
        *agents: str,
        share_skills: bool = False,
        skill_reason: str = "",
        host_reason: str = "",
    ) -> list[runtime.Launch]:
        def install_skill(_ops, agent, installed, **_kw):
            if skill_reason:
                return sandbox_bootstrap.SkillInstalled(reason=skill_reason)
            return sandbox_bootstrap.SkillInstalled(
                path=sandbox_bootstrap.sandbox_skill_destination(agent, installed)
            )

        def install_host_skill(agent, **_kw):
            calls.append(agent)
            if host_reason:
                return sandbox_bootstrap.HostSkillInstalled(reason=host_reason)
            return sandbox_bootstrap.HostSkillInstalled(
                path=str(Path.home() / f".{agent}/skills/amux/SKILL.md")
            )

        monkeypatch.setattr(
            sandbox_bootstrap,
            "install_client",
            lambda *a, **kw: sandbox_bootstrap.Installed(
                shim_path="/opt/amux", config_path="/opt/ctx.json", home="/home/agent"
            ),
        )
        monkeypatch.setattr(sandbox_bootstrap, "install_skill", install_skill)
        monkeypatch.setattr(sandbox_bootstrap, "install_host_skill", install_host_skill)
        monkeypatch.setattr(
            sandbox_bootstrap,
            "install_hooks",
            lambda _ops, agent, *a, **kw: sandbox_bootstrap.HooksInstalled(
                agent=agent,
                settings_path="/home/agent/settings.json",
                missing_kinds=(),
                location_verified=True,
            ),
        )
        monkeypatch.setattr(
            sandbox, "create", lambda name, *a, **kw: sandbox.Sandbox(name=name, id="s1")
        )
        monkeypatch.setattr(sandbox.Sandbox, "exec", lambda self, argv, **kw: "")

        rt = runtime.SandboxRuntime(
            runtime.SandboxConfig(
                resources=sandbox.Resources(share_skills=share_skills)
            )
        )
        return rt.prepare(
            specs(*[(f"%{i}", a, f"a{i}") for i, a in enumerate(agents, start=1)]),
            workspace="ws",
            task="t0",
            cwd=str(git_repo),
            socket="amux-test",
        )

    make.host_install_calls = calls  # type: ignore[attr-defined]
    return make


@pytest.fixture
def sandbox_launch(sandbox_prepare):
    """One prepared sandbox launch."""

    def make(agent: str, **kwargs) -> runtime.Launch:
        (launch,) = sandbox_prepare(agent, **kwargs)
        return launch

    return make
