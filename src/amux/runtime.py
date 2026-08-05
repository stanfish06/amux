from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from amux import sandbox, sandbox_bootstrap, store, worktree
from amux.shared import DEFAULT_SOCKET

HOST = "host"
DOCKER_SANDBOX = "docker-sandbox"

AGENT_COMMANDS = {
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
}


class GridCreationError(RuntimeError):
    """A grid failed partway through creation and was unwound."""

    def __init__(self, cause: BaseException, cleanup_failures: Sequence[str] = ()):
        self.cause = cause
        self.cleanup_failures: list[str] = list(cleanup_failures)
        super().__init__(str(cause))

    def add_cleanup_failure(self, problem: str) -> None:
        self.cleanup_failures.append(problem)

    def __str__(self) -> str:
        text = str(self.cause) or type(self.cause).__name__
        if not self.cleanup_failures:
            return text
        problems = "\n".join(f"  - {p}" for p in self.cleanup_failures)
        return f"{text}\ncleanup did not complete:\n{problems}"


@dataclass(frozen=True)
class PaneSpec:
    """A pane's identity, fixed by `core` before anything runs in it."""

    pane: str
    agent: str
    name: str


@dataclass(frozen=True)
class Launch:
    pane: str
    cwd: str = ""
    keys: tuple[str, ...] = ()


class Runtime(Protocol):
    """Prepares launches for one grid. One runtime per grid, chosen at spawn."""

    kind: str

    def preflight(
        self,
        agents: list[str],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None: ...

    def prepare(
        self,
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
        socket: str,
    ) -> list[Launch]: ...

    def resumable_names(
        self, *, workspace: str | None, task: str | None, cwd: str | None
    ) -> dict[str, list[str]]: ...

    def rollback(self) -> list[str]:
        """Unwind everything `prepare` created. Returns cleanup failures."""
        ...


class HostRuntime:
    """Agents run as host processes in per-agent git worktrees."""

    kind = HOST

    def preflight(
        self,
        agents: list[str],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None: ...

    def resumable_names(
        self, *, workspace: str | None, task: str | None, cwd: str | None
    ) -> dict[str, list[str]]:
        return {}

    def rollback(self) -> list[str]:
        return []

    def prepare(
        self,
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
        socket: str = "",
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
        """Per-agent git worktrees when the target dir is a repo."""
        if workspace and task and not cwd:
            print(
                "amux: no directory resolved for "
                f"{workspace}/{task}; agents will share one directory instead "
                "of getting a worktree each (pass -p, or spawn from the "
                "workspace directory)"
            )
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


def _context_service():
    from amux import context_service

    return context_service


GONE_RUNTIME_STATUSES = frozenset({"removed", "failed"})


def sandbox_rows(workspace: str, task: str) -> list[dict]:
    # Selects on the RUNTIME axis, never on `status`. `status` answers "was this
    # work merged"; `runtime_status` answers "does a VM exist". Asking the first
    # is how an integrated task leaked every sandbox it had.
    return [
        dict(row)
        for row in store.worktrees_for(workspace, task)
        if row["runtime"] == DOCKER_SANDBOX
        and row["sandbox_name"]
        and row["runtime_status"] not in GONE_RUNTIME_STATUSES
    ]


def sandbox_tasks(workspace: str) -> list[str]:
    seen: list[str] = []
    for row in store.worktrees_for(workspace):
        if (
            row["runtime"] == DOCKER_SANDBOX
            and row["sandbox_name"]
            and row["runtime_status"] not in GONE_RUNTIME_STATUSES
            and row["task"] not in seen
        ):
            seen.append(row["task"])
    return seen


def _retire(worktree_id: int, status: str, *, current: str) -> None:
    store.set_worktree_runtime(worktree_id, runtime_status=status)
    if current == "active":
        store.set_worktree_status(worktree_id, "removed")


def stop_task(workspace: str, task: str) -> list[str]:
    stopped: list[str] = []
    for row in sandbox_rows(workspace, task):
        name = row["sandbox_name"]
        try:
            sandbox.stop(name)
        except sandbox.SandboxError as exc:
            print(f"amux: could not stop sandbox {name}: {exc}")
            continue
        store.set_worktree_runtime(row["id"], runtime_status="stopped")
        stopped.append(name)
    return stopped


def clean_task(workspace: str, task: str, *, force: bool = False) -> list[str]:
    by_sandbox: dict[str, list[dict]] = {}
    for row in sandbox_rows(workspace, task):
        by_sandbox.setdefault(row["sandbox_name"], []).append(row)
    if not by_sandbox:
        return []

    gone = {name for name in by_sandbox if not sandbox.exists(name)}
    for name in gone:
        print(f"amux: {name} no longer exists; recording it as removed")
        _retire_all(by_sandbox.pop(name))

    live = list(by_sandbox.items())
    if not force:
        dirty = [(name, status) for name, _ in live if (status := _dirty_status(name))]
        if dirty:
            raise sandbox.SandboxError(_dirty_refusal(dirty))

    removed: list[str] = []
    stranded: list[str] = []
    for name, rows in live:
        branch = rows[0]["branch"]
        repo = rows[0]["repo"]
        handle = sandbox.Sandbox(name=name)
        was_stopped = any(r["runtime_status"] == "stopped" for r in rows)

        def give_up(problem: str) -> None:
            stranded.append(problem)
            if was_stopped:
                _restore_stopped(name)

        try:
            handle.wake()
        except sandbox.SandboxError as exc:
            give_up(
                f"{name}: could not start it to read its committed work ({exc}); "
                f"{branch} is NOT saved on the host"
            )
            continue

        source = sandbox.git_url(name, repo)
        if source is None:
            give_up(
                f"{name}: it is running but publishes no git port, so {branch} "
                "cannot be read; it is NOT saved on the host"
            )
            continue

        try:
            tip = worktree.sandbox_branch_tip(repo, name, branch, source=source)
        except worktree.WorktreeError as exc:
            give_up(
                f"{name}: cannot read {branch} to preserve it ({exc}); "
                "it is NOT saved on the host, so the sandbox was left in place"
            )
            continue

        if tip is None:
            print(f"amux: {name}: nothing committed on {branch} to preserve")
        else:
            try:
                worktree.fetch_sandbox_branch(repo, name, branch, source=source)
            except worktree.WorktreeError as exc:
                give_up(
                    f"{name}: {branch} is at {tip[:12]} but could not be fetched "
                    f"({exc}); it is NOT saved on the host"
                )
                continue

        try:
            sandbox.remove(name, force=force)
        except sandbox.SandboxError as exc:
            give_up(f"{name}: could not be removed ({exc})")
            continue

        try:
            worktree.remove_sandbox_remote(repo, name)
        except Exception as exc:  # noqa: BLE001
            print(f"amux: could not remove remote for {name}: {exc}")
        _retire_all(rows)
        removed.append(name)

    if stranded:
        raise sandbox.SandboxError(_stranded_refusal(stranded, removed))
    return removed


def _retire_all(rows: list[dict]) -> None:
    for row in rows:
        store.revoke_context_tokens_for_worktree(row["id"])
        _retire(row["id"], "removed", current=row["status"])


def _restore_stopped(name: str) -> None:
    try:
        sandbox.stop(name)
    except sandbox.SandboxError as exc:
        print(
            f"amux: {name} was started to inspect it and could not be stopped "
            f"again ({exc}); it is running"
        )


def _stranded_refusal(stranded: list[str], removed: list[str]) -> str:
    lines = ["some sandboxes could not be removed and are still on this host:"]
    lines += [f"  {item}" for item in stranded]
    if removed:
        lines.append(f"removed: {', '.join(removed)}")
    lines.append(
        "The workspace has been left in place so amux can still address them. "
        "Resolve the cause and re-run, or remove them yourself with "
        "`sbx rm -f <name>` -- which discards any work still inside them."
    )
    return "\n".join(lines)


def _dirty_status(name: str) -> str:
    try:
        return sandbox.Sandbox(name=name).working_tree_status()
    except sandbox.SandboxError as exc:
        return f"could not read the working tree: {exc}"


def _dirty_refusal(dirty: list[tuple[str, str]]) -> str:
    lines = ["refusing to remove sandboxes with uncommitted work:"]
    for name, status in dirty:
        lines.append(f"  {name}:")
        lines += [f"    {line}" for line in status.splitlines()[:20]]
    lines.append(
        "commit or discard the work inside the sandbox, or pass --force to "
        "remove it anyway and lose those changes. Committed branch tips are "
        "preserved on the host either way."
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class SandboxConfig:
    """Everything `--runtime docker-sandbox` needs beyond the grid itself."""

    resources: sandbox.Resources = field(default_factory=sandbox.Resources)
    port: int | None = None

    @property
    def resolved_port(self) -> int:
        return self.port if self.port is not None else _context_service().DEFAULT_PORT

    @property
    def policy_target(self) -> str:
        """What the user must allow: the loopback address the service binds."""
        return f"localhost:{self.resolved_port}"

    @property
    def client_endpoint(self) -> str:
        """What a sandbox dials. Docker routes this name back to the host."""
        return f"http://host.docker.internal:{self.resolved_port}"


@dataclass
class _Acquired:
    """One sandbox's resources, in the order they were acquired."""

    spec: PaneSpec
    sandbox_name: str
    repo: str = ""
    worktree_id: int | None = None
    token_id: int | None = None
    handle: sandbox.Sandbox | None = None
    hooks: sandbox_bootstrap.HooksInstalled | None = None
    reattached: bool = False


class SandboxRuntime:
    """Agents run inside per-agent Docker Sandbox microVMs."""

    kind = DOCKER_SANDBOX

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        service_healthy: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.config = config or SandboxConfig()
        self._service_healthy = service_healthy
        self._acquired: list[_Acquired] = []
        self._integration: worktree.TaskIntegration | None = None
        self.hooks: dict[str, sandbox_bootstrap.HooksInstalled] = {}

    def preflight(
        self,
        agents: list[str],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None:
        repo = worktree.repo_root(cwd) if cwd else None
        sandbox.preflight(
            agents=agents,
            repo=repo or "",
            resources=self.config.resources,
            endpoint=self.config.policy_target,
            service_healthy=self._service_healthy,
        ).raise_if_failed()

    def resumable_names(
        self, *, workspace: str | None, task: str | None, cwd: str | None
    ) -> dict[str, list[str]]:
        if not (workspace and task):
            return {}
        repo = worktree.repo_root(cwd) if cwd else None
        by_agent: dict[str, list[str]] = {}
        for row in sorted(sandbox_rows(workspace, task), key=lambda r: r["created_ts"]):
            if repo and row["repo"] != repo:
                continue
            if not row["name"]:
                continue
            by_agent.setdefault(row["agent"], []).append(row["name"])
        return by_agent

    def prepare(
        self,
        panes: list[PaneSpec],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
        socket: str = "",
    ) -> list[Launch]:
        if not (workspace and task and cwd):
            raise sandbox.SandboxError(
                "the docker-sandbox runtime needs a workspace, task and path"
            )
        repo = worktree.repo_root(cwd)
        if not repo:
            raise sandbox.SandboxError(f"{cwd} is not a git repository")

        self._integration = worktree.setup_task_integration(repo, workspace, task)

        launches = []
        for spec in panes:
            launches.append(
                self._create_one(
                    spec,
                    workspace=workspace,
                    task=task,
                    repo=repo,
                    socket=socket,
                )
            )
        return launches

    def _create_one(
        self,
        spec: PaneSpec,
        *,
        workspace: str,
        task: str,
        repo: str,
        socket: str,
    ) -> Launch:
        assert self._integration is not None
        branch = worktree.agent_branch(workspace, task, spec.name)
        name = sandbox.sandbox_name(workspace, task, spec.name, repo)
        acquired = _Acquired(spec=spec, sandbox_name=name, repo=repo)
        self._acquired.append(acquired)

        prior = self._prior_row(workspace, task, spec.name)
        existing = sandbox.find(name) if prior else None
        if existing is not None:
            handle = sandbox.Sandbox(
                name=name, id=str(existing.get("id") or ""), entry=existing
            )
            acquired.reattached = True
        else:
            if prior:
                print(
                    f"amux: sandbox {name} was recorded but no longer exists; "
                    "creating a new one (its previous contents are not recoverable)"
                )
            handle = sandbox.create(name, spec.agent, repo, self.config.resources)
        acquired.handle = handle

        if prior:
            self._supersede(workspace, task, name)

        acquired.worktree_id = store.register_worktree(
            pane=spec.pane,
            workspace=workspace,
            task=task,
            agent=spec.agent,
            name=spec.name,
            path="",
            branch=branch,
            base_ref=self._integration.base_ref,
            repo=repo,
            runtime=DOCKER_SANDBOX,
            runtime_status="created",
            sandbox_name=name,
            sandbox_id=handle.id,
            socket_name=socket or DEFAULT_SOCKET,
        )

        if acquired.reattached:
            handle.exec(["git", "checkout", branch])
        else:
            handle.exec(["git", "checkout", "-b", branch])

        plaintext, token_id = store.mint_context_token(
            acquired.worktree_id, permissions=_context_service().AGENT_PERMISSIONS
        )
        acquired.token_id = token_id
        installed = sandbox_bootstrap.install_client(
            handle, endpoint=self.config.client_endpoint, token=plaintext
        )

        # Only when the host's skills are *not* shared in. With --share-skills the
        # sandbox's skill directory is backed by the host's, and amux writing into
        # it would push a file across the boundary the wrong way — into the user's
        # own ~/.claude/skills, where `make install_skills` keeps a symlink into
        # this repository.
        if not self.config.resources.share_skills:
            skill = sandbox_bootstrap.install_skill(handle, spec.agent, installed)
            if not skill.ok:
                print(
                    f"amux: {spec.name} has no amux skill installed "
                    f"({skill.reason}); it will not know the sandbox boundary"
                )

        hooks = sandbox_bootstrap.install_hooks(handle, spec.agent, installed)
        acquired.hooks = hooks
        self.hooks[spec.pane] = hooks
        if hooks.degraded:
            version = hooks.agent_version or "version unknown"
            missing = ", ".join(hooks.missing_kinds)
            print(
                f"amux: {spec.name} ({spec.agent} {version}) cannot report "
                f"{missing}; its state will be shown as degraded"
            )

        store.set_worktree_runtime(acquired.worktree_id, runtime_status="running")
        return Launch(
            pane=spec.pane,
            cwd="",  # the pane's working directory is inside the VM
            keys=(sandbox.attach_command(name, spec.agent),),
        )

    @staticmethod
    def _prior_row(workspace: str, task: str, agent_name: str) -> dict | None:
        for row in sandbox_rows(workspace, task):
            if row["name"] == agent_name:
                return row
        return None

    @staticmethod
    def _supersede(workspace: str, task: str, sandbox_name: str) -> None:
        for row in sandbox_rows(workspace, task):
            if row["sandbox_name"] != sandbox_name:
                continue
            store.revoke_context_tokens_for_worktree(row["id"])
            if row["status"] == "active":
                store.set_worktree_status(row["id"], "removed")

    def rollback(self) -> list[str]:
        problems: list[str] = []
        for acquired in reversed(self._acquired):
            problems.extend(self._release(acquired))
        self._acquired.clear()
        if self._integration is not None:
            try:
                worktree.remove_task_integration(self._integration)
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                problems.append(f"integration worktree: {exc}")
            self._integration = None
        return problems

    def _release(self, acquired: _Acquired) -> list[str]:
        problems: list[str] = []
        if acquired.token_id is not None:
            try:
                store.revoke_context_token(acquired.token_id)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{acquired.sandbox_name}: revoke token: {exc}")
        if acquired.handle is not None and not acquired.reattached:
            try:
                sandbox.remove(acquired.sandbox_name, force=True)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{acquired.sandbox_name}: remove sandbox: {exc}")
            if acquired.repo:
                try:
                    worktree.remove_sandbox_remote(acquired.repo, acquired.sandbox_name)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{acquired.sandbox_name}: remove remote: {exc}")
        if acquired.worktree_id is not None:
            try:
                store.set_worktree_runtime(
                    acquired.worktree_id, runtime_status="failed"
                )
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{acquired.sandbox_name}: mark runtime failed: {exc}")
            try:
                store.set_worktree_status(acquired.worktree_id, "removed")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{acquired.sandbox_name}: mark row removed: {exc}")
        return problems
