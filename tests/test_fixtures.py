"""The fixtures themselves.

Three other phases build on `fake_sbx`, `git_factory`, and the state isolation
guard. An untested fixture that silently stops isolating, or a fake `sbx` that
quietly matches the wrong argv, would show up as confusing failures in someone
else's module — so the scaffolding is pinned here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from amux import shared, store


# --- state isolation ---


def test_state_dir_is_redirected_away_from_the_real_one() -> None:
    real = Path("~/.local/state/amux").expanduser()
    assert shared.STATE_DIR != real
    assert store.DB_PATH != real / "context.db"


def test_the_live_database_is_never_touched(db_path: Path) -> None:
    """amux is developed from inside a live amux session. A test that resolved
    the real `context.db` would corrupt the session running it."""
    store.register_worktree(
        pane="%1",
        workspace="proj",
        task="task0",
        path="/tmp/wt",
        branch="b",
        db_path=db_path,
    )
    assert db_path.exists()
    assert "state/amux/context.db" in str(db_path)


def test_no_module_still_points_at_the_real_state_dir(isolate_state: Path) -> None:
    """`from amux.shared import STATE_DIR` copies the value into the importing
    module, so patching `shared.STATE_DIR` alone never reached `worktree` or
    `events`. That let `worktree.setup_task` create and delete worktrees in the
    live state directory. Discovered dynamically, so a module that starts
    importing STATE_DIR tomorrow is covered without editing a list."""
    import sys

    real = Path("~/.local/state/amux").expanduser()
    leaked = {
        name
        for name, module in list(sys.modules.items())
        if (name == "amux" or name.startswith("amux."))
        and module is not None
        and getattr(module, "STATE_DIR", None) == real
    }
    assert not leaked, f"modules still bound to the live state dir: {leaked}"


def test_worktree_paths_resolve_inside_the_sandbox(isolate_state: Path) -> None:
    """The concrete consequence of the bug above: this is the call that would
    have written to the directory holding the running agents' worktrees."""
    from amux import worktree

    root = Path(worktree.task_worktree_root("ws", "t0"))
    assert isolate_state in root.parents
    assert "/Users/stan/.local/state/amux/worktrees" not in str(root)


def test_default_db_path_also_lands_in_the_sandbox(isolate_state: Path) -> None:
    """Calls that pass no `db_path` must still be isolated, since most
    production call sites do exactly that."""
    store.register_worktree(
        pane="%1", workspace="proj", task="task0", path="/tmp/wt", branch="b"
    )
    assert (isolate_state / "context.db").exists()


# --- git repositories ---


def test_git_repo_has_a_commit_on_main(git_repo: Path, git_run) -> None:
    assert (git_repo / ".git").exists()
    assert git_run(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git_run(git_repo, "rev-list", "--count", "HEAD") == "1"


def test_empty_repo_has_no_commits(git_factory, git_run) -> None:
    """amux skips worktree isolation for a repo with no commits, so that state
    needs to be constructible."""
    repo = git_factory(empty=True)
    assert (repo / ".git").exists()
    with pytest.raises(subprocess.CalledProcessError):
        git_run(repo, "rev-parse", "HEAD")


def test_repositories_are_independent(git_factory, git_run) -> None:
    a, b = git_factory(), git_factory()
    assert a != b
    assert git_run(a, "rev-parse", "HEAD") != git_run(b, "rev-parse", "HEAD")


# --- pane facts ---


def test_live_facts_carry_a_boundary(make_facts) -> None:
    facts = make_facts(alive=True, created=500.0)
    assert facts.boundary == 500.0


def test_dead_facts_have_no_boundary(make_facts) -> None:
    assert make_facts(alive=False, created=None).boundary is None


def test_live_facts_get_a_creation_time_by_default(make_facts) -> None:
    """`PaneFacts` refuses to construct a live pane without one, so the fixture
    must supply it rather than making every caller remember."""
    assert make_facts(alive=True).boundary is not None


# --- fake sbx ---


def _sbx(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sbx", *args], capture_output=True, text=True)


def test_fake_sbx_is_first_on_path(fake_sbx) -> None:
    import shutil

    assert shutil.which("sbx") == str(fake_sbx.bin_dir / "sbx")


def test_fake_sbx_records_the_exact_argv(fake_sbx) -> None:
    """These recorded argv lists are how the suite pins Docker's command
    surface without Docker. If the adapter drifts, this is what notices."""
    fake_sbx.respond("create", stdout="ok\n")
    _sbx("create", "--clone", "--name", "box", "--cpus", "2", "claude", "/repo")

    assert fake_sbx.calls == [
        ["create", "--clone", "--name", "box", "--cpus", "2", "claude", "/repo"]
    ]
    assert fake_sbx.called_with("create", "--clone")
    assert not fake_sbx.called_with("rm")


def test_fake_sbx_replays_scripted_output(fake_sbx) -> None:
    fake_sbx.respond_json("ls", "--json", payload={"sandboxes": [{"name": "box"}]})
    result = _sbx("ls", "--json")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"sandboxes": [{"name": "box"}]}


def test_fake_sbx_replays_failures(fake_sbx) -> None:
    fake_sbx.respond("rm", stderr="ERROR: sandbox in use\n", returncode=1)
    result = _sbx("rm", "box")
    assert result.returncode == 1
    assert "sandbox in use" in result.stderr


def test_fake_sbx_matches_the_first_registered_prefix(fake_sbx) -> None:
    fake_sbx.respond("policy", "ls", stdout="specific\n")
    fake_sbx.respond("policy", stdout="general\n")
    assert _sbx("policy", "ls").stdout == "specific\n"
    assert _sbx("policy", "init", "balanced").stdout == "general\n"


def test_unscripted_calls_fail_loudly(fake_sbx) -> None:
    """Silence would let an adapter issue an unreviewed command and still pass."""
    result = _sbx("create", "claude", ".")
    assert result.returncode == 127
    assert "no scripted response" in result.stderr


def test_no_sbx_fixture_hides_the_executable(no_sbx) -> None:
    import shutil

    assert shutil.which("sbx") is None


def test_the_guard_blocks_a_real_sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `fake_sbx` there is a real `sbx` on this developer's PATH. The
    autouse guard must stop a test reaching it, or the offline guarantee decays
    the moment someone installs Docker Sandboxes."""
    monkeypatch.delenv("FAKE_SBX_LOG", raising=False)
    with pytest.raises(AssertionError, match="use the fake_sbx fixture"):
        subprocess.run(["sbx", "ls"], capture_output=True)


def test_the_guard_blocks_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolvable `docker` must be refused.

    This previously asserted that plain `docker` raised, which passed only
    because the guard treated *unresolvable* as *real* — and docker is not
    installed on this machine. It was testing the bug, not the guard. So put a
    real executable on PATH and block that.
    """
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(AssertionError, match="use the fake_sbx fixture"):
        subprocess.run(["docker", "ps"], capture_output=True)


def test_the_guard_leaves_other_commands_alone(git_repo: Path) -> None:
    result = subprocess.run(["git", "status"], cwd=git_repo, capture_output=True)
    assert result.returncode == 0


# --- events.publish_state ---


def test_publish_state_sets_the_option_and_wakes_waiters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context service needs this half of `emit` without the store write
    beside it, because it attributes events from a token rather than from
    whichever worktree the pane fronts now."""
    from amux import events

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(events, "_tmux", lambda socket, *a: calls.append((socket, *a)))

    events.publish_state("%7", "needs-input", "amux-root")

    assert calls == [
        ("amux-root", "set-option", "-p", "-t", "%7", events.STATE_OPTION, "needs-input"),
        ("amux-root", "wait-for", "-S", "amux-state-7"),
    ]


def test_emit_and_publish_state_move_the_pane_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One implementation, so the option name and wait channel cannot drift
    between the hook path and the service path."""
    from amux import events

    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(events, "_tmux", lambda socket, *a: seen.append((socket, *a)))
    monkeypatch.setattr(events, "_amux_socket", lambda: "amux-root")
    monkeypatch.setattr(
        events, "pane_facts", lambda pane, socket=None: events.PaneFacts(
            alive=True, created=1.0
        )
    )
    monkeypatch.setattr(events, "resolve_scope", lambda pane, facts=None: ("ws", "t0"))

    events.emit("notify", pane="%7", socket="amux-root")
    via_emit = list(seen)

    seen.clear()
    events.publish_state("%7", "needs-input", "amux-root")

    assert via_emit == seen


def test_no_sbx_lets_the_caller_meet_a_missing_executable(no_sbx) -> None:
    """`no_sbx` simulates a machine without Docker Sandboxes, so code under
    test must meet FileNotFoundError — the guard used to raise "invoked real
    sbx" instead, which made the fixture unusable with any path that actually
    shells out. An unresolvable name is the one case that certainly is not a
    real binary."""
    with pytest.raises(FileNotFoundError):
        subprocess.run(["sbx", "version"], capture_output=True)


def test_the_guard_still_blocks_a_real_sbx_alongside_the_fake(fake_sbx) -> None:
    """Relaxing the guard must not let a real binary through by absolute path
    while the fake is installed."""
    with pytest.raises(AssertionError, match="use the fake_sbx fixture"):
        subprocess.run(["/opt/homebrew/bin/sbx", "ls"], capture_output=True)
