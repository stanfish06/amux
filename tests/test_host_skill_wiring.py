"""Spawning a host grid installs amux's skill (`HostRuntime.prepare`).

Group 1 proved the write itself. This is the wiring: which agents trigger it,
how often, what gets said about it, and -- the part that matters most -- that
none of it can cost anyone their grid. The document informs an agent; it is not
what makes the agent work.

`$HOME` is redirected by the autouse `isolate_home` fixture in conftest, so
these tests exercise the real, un-injected `install_host_skill` call the runtime
makes without going anywhere near the developer's own skill directory.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

import fake_tmux
from amux import core, runtime, sandbox_bootstrap, sandbox_hooks
from test_host_grid_snapshot import tmux_calls  # noqa: F401 - fixture


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


def skill(home: Path, agent: str) -> Path:
    return home / f".{agent}" / "skills" / "amux" / "SKILL.md"


def prepare(panes, cwd) -> None:
    runtime.HostRuntime().prepare(panes, workspace="ws", task="t0", cwd=str(cwd))


# --- which agents get one ----------------------------------------------------


def test_preparing_a_host_grid_installs_the_skill_for_every_agent_kind(
    git_repo, isolate_home
):
    prepare(specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")), git_repo)

    for agent in ("claude", "codex"):
        assert skill(isolate_home, agent).read_bytes() == (
            sandbox_bootstrap.skill_source().read_bytes()
        )


def test_a_raw_command_spec_installs_nothing(git_repo, isolate_home):
    """`amux spg ws shell -a bash`: amux does not know where bash reads skills."""
    prepare(specs(("%1", "bash", "alpha")), git_repo)

    assert list(isolate_home.iterdir()) == []


def test_a_raw_command_beside_an_agent_installs_only_the_agent_s(
    git_repo, isolate_home
):
    prepare(specs(("%1", "claude", "alpha"), ("%2", "bash", "beta")), git_repo)

    assert skill(isolate_home, "claude").is_file()
    assert not (isolate_home / ".codex").exists()


def test_the_agents_amux_launches_are_the_agents_it_can_install_for():
    """Two modules answer 'is this an amux agent'. A divergence would either
    warn on every spawn or silently skip an agent that has a skill directory."""
    assert set(runtime.AGENT_COMMANDS) == set(sandbox_hooks.HOOKS_BY_AGENT)


# --- once per kind, not once per pane ----------------------------------------


def test_four_claude_agents_resolve_one_destination_and_write_once(
    git_repo, isolate_home, monkeypatch
):
    calls: list[str] = []
    real = sandbox_bootstrap.install_host_skill

    def counted(agent, **kwargs):
        calls.append(agent)
        return real(agent, **kwargs)

    monkeypatch.setattr(runtime.sandbox_bootstrap, "install_host_skill", counted)
    prepare(specs(*[(f"%{i}", "claude", f"a{i}") for i in range(1, 5)]), git_repo)

    assert calls == ["claude"]


def test_a_mixed_grid_writes_once_per_kind(git_repo, isolate_home, monkeypatch):
    calls: list[str] = []
    real = sandbox_bootstrap.install_host_skill
    monkeypatch.setattr(
        runtime.sandbox_bootstrap,
        "install_host_skill",
        lambda agent, **kw: (calls.append(agent), real(agent, **kw))[1],
    )
    prepare(
        specs(
            ("%1", "claude", "a"),
            ("%2", "codex", "b"),
            ("%3", "claude", "c"),
            ("%4", "codex", "d"),
        ),
        git_repo,
    )

    assert calls == ["claude", "codex"]


# --- what gets said about it -------------------------------------------------


def test_replacing_a_checkout_symlink_names_the_path(
    git_repo, isolate_home, capsys, tmp_path
):
    """The developer whose edits just stopped taking effect has to be told why."""
    checkout = tmp_path / "checkout" / "skills" / "amux"
    checkout.mkdir(parents=True)
    (checkout / "SKILL.md").write_text("the checkout copy\n")
    skills = isolate_home / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "amux").symlink_to(checkout)

    prepare(specs(("%1", "claude", "alpha")), git_repo)

    assert str(skill(isolate_home, "claude")) in capsys.readouterr().out
    assert (checkout / "SKILL.md").read_text() == "the checkout copy\n"


def test_a_spawn_that_changes_nothing_says_nothing(git_repo, isolate_home, capsys):
    prepare(specs(("%1", "claude", "alpha")), git_repo)
    capsys.readouterr()

    prepare(specs(("%1", "claude", "alpha")), git_repo)

    assert "skill" not in capsys.readouterr().out


def test_a_failure_names_every_agent_that_wanted_it(
    git_repo, isolate_home, capsys, monkeypatch
):
    """One destination, but the operator needs to know which agents lost out."""
    monkeypatch.setattr(
        runtime.sandbox_bootstrap,
        "install_host_skill",
        lambda agent, **kw: sandbox_bootstrap.HostSkillInstalled(reason="disk on fire"),
    )
    prepare(specs(("%1", "claude", "alpha"), ("%2", "claude", "beta")), git_repo)

    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out
    assert out.count("disk on fire") == 2


def test_an_unwritable_destination_reports_and_the_launches_still_come_back(
    git_repo, isolate_home, capsys
):
    claude = isolate_home / ".claude"
    claude.mkdir()
    claude.chmod(0o500)
    try:
        launches = runtime.HostRuntime().prepare(
            specs(("%1", "claude", "alpha")),
            workspace="ws",
            task="t0",
            cwd=str(git_repo),
        )
    finally:
        claude.chmod(0o700)

    assert [launch.pane for launch in launches] == ["%1"]
    assert launches[0].keys[-1].startswith(runtime.AGENT_COMMANDS["claude"])
    assert "alpha" in capsys.readouterr().out


def test_an_unresolvable_source_document_reports_and_the_grid_comes_up(
    git_repo, isolate_home, capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "amux.sandbox_client.__file__", str(tmp_path / "a" / "b" / "sandbox_client.py")
    )
    launches = runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=str(git_repo)
    )

    assert [launch.pane for launch in launches] == ["%1"]
    assert "SKILL.md" in capsys.readouterr().out


class ClosedStdout:
    """A stdout that has gone away, as `amux spw ws | head -1` leaves it."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_closed_stdout_does_not_tear_the_grid_down(
    git_repo, isolate_home, monkeypatch, tmux_calls
):
    """The regression this whole degradation story exists to prevent.

    `amux spw ws | head -1` closes stdout after the first line. The install's
    own report is the next `print`, which then raises `BrokenPipeError` -- and
    anything escaping `HostRuntime.prepare` becomes a `GridCreationError`, which
    `_build_grid`'s caller answers by killing the session. Reporting on a
    markdown file must not be able to do that.
    """
    random.seed(1234)
    monkeypatch.setattr(sys, "stdout", ClosedStdout())

    grid = core._build_grid(  # noqa: SLF001
        fake_tmux.new_window(), 1, 1, ["claude"], str(git_repo),
        workspace="ws", task="t0", runtime=runtime.HostRuntime(),
    )

    monkeypatch.undo()
    assert [pane.name for pane in grid.agent_panes] != []
    assert skill(isolate_home, "claude").is_file()


def test_an_exception_from_the_install_loop_degrades_rather_than_raising(
    git_repo, isolate_home, capsys, monkeypatch
):
    """`install_host_skill` returns rather than raises, so what is left to guard
    is the loop and its reporting -- and a guard is only worth having if it
    holds for a failure nobody predicted."""
    def blow_up(*_args, **_kwargs) -> object:
        raise RuntimeError("something nobody wrote a catch for")

    monkeypatch.setattr(runtime.sandbox_bootstrap, "install_host_skill", blow_up)

    launches = runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=str(git_repo)
    )

    assert [launch.pane for launch in launches] == ["%1"]
    assert "something nobody wrote a catch for" in capsys.readouterr().out


# --- independent of the target directory -------------------------------------


def test_a_grid_in_a_directory_that_is_not_a_repository_still_installs(
    tmp_path, isolate_home
):
    """The destination is under `$HOME`; it owes nothing to the target repo."""
    plain = tmp_path / "plain"
    plain.mkdir()

    prepare(specs(("%1", "claude", "alpha")), plain)

    assert skill(isolate_home, "claude").is_file()


def test_a_grid_with_no_resolved_directory_at_all_still_installs(isolate_home):
    runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=None
    )

    assert skill(isolate_home, "claude").is_file()


# --- not part of rollback ----------------------------------------------------


def test_the_host_runtime_has_nothing_to_unwind(git_repo, isolate_home):
    """The document is shared with every other grid on the machine, so removing
    it during one grid's unwind would take it away from healthy ones."""
    host = runtime.HostRuntime()
    host.prepare(specs(("%1", "claude", "alpha")), workspace="ws", task="t0",
                 cwd=str(git_repo))

    assert host.rollback() == []
    assert skill(isolate_home, "claude").is_file()


class ExplodingRuntime:
    """A runtime that fails during `prepare`, the way a sandbox create can."""

    kind = "exploding"

    def preflight(self, agents, *, workspace, task, cwd): ...

    def resumable_names(self, *, workspace, task, cwd):
        return {}

    def rollback(self):
        return []

    def prepare(self, panes, *, workspace, task, cwd, socket=""):
        raise RuntimeError("insufficient memory for sandbox")


def test_an_installed_document_survives_an_unwound_grid(
    git_repo, isolate_home, tmux_calls
):
    random.seed(1234)
    core._build_grid(  # noqa: SLF001
        fake_tmux.new_window(), 1, 1, ["claude"], str(git_repo),
        workspace="ws", task="t0", runtime=runtime.HostRuntime(),
    )
    installed = skill(isolate_home, "claude")
    assert installed.is_file()

    random.seed(1234)
    with pytest.raises(runtime.GridCreationError):
        core._build_grid(  # noqa: SLF001
            fake_tmux.new_window(), 1, 1, ["claude"], str(git_repo),
            workspace="ws", task="t1", runtime=ExplodingRuntime(),
        )

    assert installed.read_bytes() == sandbox_bootstrap.skill_source().read_bytes()
