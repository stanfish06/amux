"""Golden snapshot of host-runtime grid building.

Phase 4 moves launch preparation out of `core._build_grid` and behind a runtime
seam, and splits `worktree.setup_task`. Neither is allowed to change what the
host runtime actually does. These tests record every tmux mutation, every
registry row, and every event the host path produces and compare them against
committed goldens captured from the pre-refactor code, so any drift in pane
metadata, hook wiring, worktree layout, send-keys order, or event attribution
fails loudly instead of silently.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import fake_tmux
from amux import core, events, store, worktree

GOLDEN_DIR = Path(__file__).parent / "golden"

GIT_ENV = (
    "-c",
    "user.email=test@amux.invalid",
    "-c",
    "user.name=amux test",
    "-c",
    "commit.gpgsign=false",
)


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_ENV, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("seed\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "seed")
    return path


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry and worktree root at this test's tmp dir."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(store, "DB_PATH", state / "context.db")
    monkeypatch.setattr(worktree, "STATE_DIR", state)
    return state


@pytest.fixture
def tmux_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Capture the raw tmux calls `events.emit` makes instead of running them.

    Returning None from `_tmux_out` makes every pane look not-alive, which is
    what a pane on a server this test never started really is; `emit` then
    resolves scope from the registry, exercising the durable-identity path.
    """
    calls: list[tuple] = []

    def fake_tmux_cmd(socket: str, *args: str) -> None:
        calls.append(("tmux", socket, *args))

    def fake_tmux_out(socket: str, *args: str) -> None:
        calls.append(("tmux-query", socket, *args))
        return None

    monkeypatch.setattr(events, "_tmux", fake_tmux_cmd)
    monkeypatch.setattr(events, "_tmux_out", fake_tmux_out)
    return calls


_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def _scrub(value, subs: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        for needle, replacement in subs:
            value = value.replace(needle, replacement)
        return _SHA_RE.sub("<SHA>", value)
    if isinstance(value, (list, tuple)):
        return [_scrub(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, subs) for k, v in value.items()}
    return value


def snapshot(
    window: fake_tmux.FakeWindow,
    tmux_calls: list[tuple],
    grid: core.AgentGrid,
    *,
    repo: Path,
    state: Path,
):
    """Everything the host path did, with volatile values normalized away."""
    subs = [(str(state), "<STATE>"), (str(repo), "<REPO>")]
    rows = store.worktrees_for("ws", "t0")
    with store._connect() as conn:  # noqa: SLF001 - the registry is the contract
        raw_events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")]
    return _scrub(
        {
            "tmux": [list(c) for c in window.server.log],
            "events_tmux": [list(c) for c in tmux_calls],
            "worktrees": [
                {k: v for k, v in row.items() if k not in ("id", "created_ts")}
                for row in rows
            ],
            "events": [
                {
                    k: v
                    for k, v in row.items()
                    if k not in ("id", "ts", "worktree_id")
                }
                for row in raw_events
            ],
            "grid": {
                "cwd": grid.cwd,
                "task_name": grid.task_name,
                "panes": [
                    {
                        "pane": p.pane.id,
                        "cwd": p.cwd,
                        "agent_name": p.agent_name,
                        "label": p.label,
                        "name": p.name,
                        "state": p.state,
                        "is_agent": p.is_agent,
                    }
                    for p in grid.agent_panes
                ],
            },
        },
        subs,
    )


def assert_golden(name: str, actual: dict) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    serialized = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized)
        pytest.fail(f"wrote missing golden {path.name}; re-run to verify")
    assert serialized == path.read_text()


def build(window, cwd, workspace="ws", task="t0", agents=None, shape=(2, 2)):
    random.seed(1234)
    return core._build_grid(  # noqa: SLF001 - the function under characterization
        window,
        shape[0],
        shape[1],
        agents or ["claude", "codex", "echo hello", "claude"],
        cwd,
        workspace=workspace,
        task=task,
    )


def test_grid_in_a_repo(repo, isolated_state, tmux_calls):
    """The full host path: worktrees, branches, registry rows, launches."""
    window = fake_tmux.new_window()
    grid = build(window, str(repo))
    assert_golden(
        "grid_in_repo",
        snapshot(window, tmux_calls, grid, repo=repo, state=isolated_state),
    )
    # The goldens above encode the paths; assert the worktrees really exist so a
    # golden can never pass on a path that git never created.
    for row in store.worktrees_for("ws", "t0"):
        assert Path(row["path"]).is_dir()
    int_path = Path(worktree.task_worktree_root("ws", "t0")) / worktree.INTEGRATION_DIR
    assert int_path.is_dir()


def test_grid_outside_a_repo(tmp_path, isolated_state, tmux_calls):
    """A non-repo target keeps the shared-directory behavior, no registry rows."""
    plain = tmp_path / "plain"
    plain.mkdir()
    window = fake_tmux.new_window()
    grid = build(window, str(plain))
    snap = snapshot(window, tmux_calls, grid, repo=plain, state=isolated_state)
    assert snap["worktrees"] == []
    assert_golden("grid_outside_repo", snap)


def test_grid_in_a_repo_without_commits(tmp_path, isolated_state, tmux_calls, capsys):
    """Worktree setup fails soft: agents still launch in the shared directory."""
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    window = fake_tmux.new_window()
    grid = build(window, str(empty))
    assert "worktree isolation unavailable: repo has no commits yet" in capsys.readouterr().out
    snap = snapshot(window, tmux_calls, grid, repo=empty, state=isolated_state)
    assert snap["worktrees"] == []
    assert_golden("grid_repo_no_commits", snap)


def test_grid_without_workspace_or_task(repo, isolated_state, tmux_calls):
    """`_build_grid` only isolates when it knows the workspace and task."""
    window = fake_tmux.new_window()
    random.seed(1234)
    grid = core._build_grid(  # noqa: SLF001
        window, 1, 2, ["claude", "codex"], str(repo)
    )
    snap = snapshot(window, tmux_calls, grid, repo=repo, state=isolated_state)
    assert snap["worktrees"] == []
    assert_golden("grid_no_scope", snap)


def test_grid_without_cwd(repo, isolated_state, tmux_calls):
    """No cwd: panes fall back to their own current path and stay shared."""
    window = fake_tmux.new_window()
    random.seed(1234)
    grid = core._build_grid(  # noqa: SLF001
        window, 1, 2, ["claude", "codex"], None, workspace="ws", task="t0"
    )
    snap = snapshot(window, tmux_calls, grid, repo=repo, state=isolated_state)
    assert snap["worktrees"] == []
    assert_golden("grid_no_cwd", snap)
