"""The execution-runtime seam.

`core` owns pane identity: it splits the grid, names each pane, and stamps the
tmux metadata that everything else keys on. What a pane then *runs*, and from
which directory, is the runtime's decision.

Today there is one runtime. `HostRuntime` reproduces the historical behavior:
give each agent its own git worktree when the target is a repo, `cd` into it,
and start the agent command. A Docker Sandbox runtime lands behind the same
`prepare()` call and returns an attach command instead of a local `cd`, so
`core` never grows a second launch path.

The seam is deliberately one-way. A runtime is handed pane identities and
returns `Launch` values; it never touches tmux, never emits events, and never
sees libtmux objects. That keeps pane metadata, hook wiring and event
attribution identical no matter which runtime prepared the launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from amux import worktree

HOST = "host"
DOCKER_SANDBOX = "docker-sandbox"

AGENT_COMMANDS = {
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
}


@dataclass(frozen=True)
class PaneSpec:
    """A pane's identity, fixed by `core` before anything runs in it."""

    pane: str
    agent: str
    name: str


@dataclass(frozen=True)
class Launch:
    """What one pane runs, and from where.

    `cwd` is the directory the pane ends up working in; empty means the runtime
    has no opinion and the pane keeps its own. `keys` are shell lines sent in
    order — the host runtime sends a `cd` then the agent command, and a raw
    command with no worktree sends just itself.
    """

    pane: str
    cwd: str = ""
    keys: tuple[str, ...] = ()


class Runtime(Protocol):
    """Prepares launches for one grid. One runtime per grid, chosen at spawn."""

    kind: str

    def prepare(
        self,
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> list[Launch]: ...


class HostRuntime:
    """Agents run as host processes in per-agent git worktrees."""

    kind = HOST

    def prepare(
        self,
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> list[Launch]:
        paths = self._worktrees(panes, workspace=workspace, task=task, cwd=cwd)
        launches = []
        for spec in panes:
            command = AGENT_COMMANDS.get(spec.agent, spec.agent)
            path = paths.get(spec.pane)
            keys = []
            if path:
                keys.append(worktree.shell_cd(path))
            if command:
                keys.append(command)
            launches.append(
                Launch(pane=spec.pane, cwd=path or cwd or "", keys=tuple(keys))
            )
        return launches

    @staticmethod
    def _worktrees(
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> dict[str, str]:
        """Per-agent git worktrees when the target dir is a repo. Fail soft: a
        non-repo target keeps today's shared-directory behavior."""
        if not (workspace and task and cwd):
            return {}
        repo = worktree.repo_root(cwd)
        if not repo:
            return {}
        try:
            return worktree.setup_task(
                repo,
                workspace,
                task,
                [(spec.pane, spec.agent, spec.name) for spec in panes],
            )
        except worktree.WorktreeError as exc:
            print(f"amux: worktree isolation unavailable: {exc}")
            return {}
