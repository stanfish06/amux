"""Shared fixtures for the amux test suite.

Three rules shape everything here:

1. **Nothing touches the real state.** amux is developed from inside a live amux
   session, so a test that resolves `$XDG_STATE_HOME/amux/context.db` would
   corrupt the very database coordinating the agents running the test. The
   `isolate_state` fixture is autouse and unconditional.
2. **Nothing touches the network, Docker, or a model provider.** The whole suite
   runs offline. `sbx` is a fake executable placed on `PATH`.
3. **Nothing touches tmux.** Pane facts are built directly rather than queried,
   so tests never need a running tmux server.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from amux import events, shared, store, worktree

# --- state isolation ---


@pytest.fixture(autouse=True)
def isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every state-directory consumer at a per-test scratch directory.

    Autouse and unconditional. `STATE_DIR` and `DB_PATH` are resolved at import
    time, so setting `XDG_STATE_HOME` alone would be silently ineffective for
    any module already imported — both constants are patched as well.

    `worktree` and `events` do `from amux.shared import STATE_DIR`, which binds
    their own module-level name; patching `shared.STATE_DIR` does not reach
    them. They are patched individually because `worktree.task_worktree_root`
    would otherwise create real worktrees under the developer's live amux state
    directory — the one coordinating the agents running the suite.
    """
    state = tmp_path / "state"
    (state / "amux").mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(shared, "STATE_DIR", state / "amux")
    monkeypatch.setattr(worktree, "STATE_DIR", state / "amux")
    monkeypatch.setattr(events, "STATE_DIR", state / "amux")
    monkeypatch.setattr(store, "DB_PATH", state / "amux" / "context.db")
    return state / "amux"


@pytest.fixture
def db_path(isolate_state: Path) -> Path:
    """An isolated `context.db`. Absent until the first `store` call creates it."""
    return isolate_state / "context.db"


# --- git repositories ---


def _git(repo: Path, *args: str) -> str:
    """Run git with a fixed identity and no user config, so a developer's
    global `commit.gpgsign` or `init.defaultBranch` cannot change results."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "amux tests",
        "GIT_AUTHOR_EMAIL": "tests@amux.invalid",
        "GIT_COMMITTER_NAME": "amux tests",
        "GIT_COMMITTER_EMAIL": "tests@amux.invalid",
    }
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def git_factory(tmp_path: Path):
    """Build disposable git repositories with a real initial commit.

    A repo with no commits takes a different amux code path (worktree isolation
    is skipped), so the default here is deliberately *committed*.
    """
    counter = {"n": 0}

    def make(name: str | None = None, *, empty: bool = False) -> Path:
        counter["n"] += 1
        repo = tmp_path / "repos" / (name or f"repo{counter['n']}")
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        if not empty:
            (repo / "README.md").write_text(f"# {repo.name}\n")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-qm", "initial commit")
        return repo

    return make


@pytest.fixture
def git_repo(git_factory) -> Path:
    """One disposable git repository with a single commit on `main`."""
    return git_factory()


@pytest.fixture
def git_run():
    """Run a git command inside a test repository and return its stdout."""
    return _git


# --- tmux pane facts ---


@pytest.fixture
def make_facts():
    """Build `PaneFacts` without tmux.

    A live pane must carry a creation time (`PaneFacts.__post_init__` enforces
    it), so one is supplied by default; pass `created=` to control the event
    boundary a test exercises.
    """

    def make(
        alive: bool | None = True,
        *,
        created: float | None = None,
        kind: events.PaneKind | None = "amux",
        **kwargs: Any,
    ) -> events.PaneFacts:
        if alive and created is None:
            created = 1_000.0
        return events.PaneFacts(alive=alive, kind=kind, created=created, **kwargs)

    return make


# --- fake sbx ---

_FAKE_SBX = '''\
#!/usr/bin/env python3
"""Stand-in for Docker's `sbx`. Logs argv, replays scripted responses."""
import json
import os
import sys

log = os.environ["FAKE_SBX_LOG"]
script = os.environ["FAKE_SBX_SCRIPT"]

argv = sys.argv[1:]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\\n")

with open(script) as fh:
    responses = json.load(fh)

for entry in responses:
    prefix = entry["argv"]
    if argv[: len(prefix)] == prefix:
        sys.stdout.write(entry.get("stdout", ""))
        sys.stderr.write(entry.get("stderr", ""))
        sys.exit(entry.get("returncode", 0))

sys.stderr.write("fake sbx: no scripted response for %s\\n" % " ".join(argv))
sys.exit(127)
'''


@dataclass
class FakeSbx:
    """An `sbx` executable on `PATH` that records calls and replays responses."""

    bin_dir: Path
    log: Path
    script: Path
    _responses: list[dict[str, Any]] = field(default_factory=list)

    def respond(
        self,
        *argv: str,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        """Reply to any invocation starting with `argv`. First match wins, so
        register specific prefixes before general ones."""
        self._responses.append(
            {
                "argv": list(argv),
                "stdout": stdout,
                "stderr": stderr,
                "returncode": returncode,
            }
        )
        self.script.write_text(json.dumps(self._responses))

    def respond_json(self, *argv: str, payload: Any, **kwargs: Any) -> None:
        self.respond(*argv, stdout=json.dumps(payload), **kwargs)

    @property
    def calls(self) -> list[list[str]]:
        """Every argv the adapter passed to `sbx`, in order."""
        if not self.log.exists():
            return []
        return [
            json.loads(line)["argv"]
            for line in self.log.read_text().splitlines()
            if line
        ]

    def called_with(self, *argv: str) -> bool:
        return any(call[: len(argv)] == list(argv) for call in self.calls)


@pytest.fixture
def fake_sbx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSbx:
    """Put a fake `sbx` first on `PATH`.

    Asserting on `fake.calls` is how the suite pins the exact Docker Sandbox
    command surface without Docker installed. The real CLI is an evolving
    external dependency; these recorded argv lists are the contract.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "sbx-calls.jsonl"
    script = tmp_path / "sbx-responses.json"
    script.write_text("[]")

    exe = bin_dir / "sbx"
    exe.write_text(textwrap.dedent(_FAKE_SBX).replace("/usr/bin/env python3", sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SBX_LOG", str(log))
    monkeypatch.setenv("FAKE_SBX_SCRIPT", str(script))
    return FakeSbx(bin_dir=bin_dir, log=log, script=script)


@pytest.fixture
def no_sbx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A `PATH` with no `sbx` on it, for preflight-failure tests."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


# --- guards ---


@pytest.fixture(autouse=True)
def no_real_sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test reaches for a real `sbx` or `docker`.

    Without this, a suite written against the fake would quietly start
    exercising the developer's actual Docker install once one is present, and
    the offline guarantee in the spec would erode without anyone noticing.
    """
    real_run = subprocess.run

    def guarded(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        argv0 = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else cmd
        name = Path(str(argv0)).name
        if name in {"sbx", "docker"}:
            resolved = shutil.which(str(argv0))
            fake = os.environ.get("FAKE_SBX_LOG")
            if not fake or resolved is None or "bin/sbx" not in resolved:
                raise AssertionError(
                    f"test invoked real {name!r}; use the fake_sbx fixture"
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
