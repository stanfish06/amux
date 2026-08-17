from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from amux import apple_container, sandbox, sandbox_bootstrap, store, worktree
from amux.shared import (
    DEFAULT_SOCKET,
    AgentRequest,
    render_command,
    render_tuning,
    report,
    skill_bootstrap_message,
    skill_pointer_args,
)

HOST = "host"
DOCKER_SANDBOX = "docker-sandbox"
APPLE_CONTAINER = "apple-container"

AGENT_COMMANDS = {
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
}


class GridCreationError(RuntimeError):
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
    pane: str
    agent: str
    name: str
    model: str = ""
    effort: str = ""

    @property
    def request(self) -> AgentRequest:
        return AgentRequest(agent=self.agent, model=self.model, effort=self.effort)


def install_host_skills(
    panes: Sequence[PaneSpec],
) -> dict[str, sandbox_bootstrap.HostSkillInstalled]:
    results: dict[str, sandbox_bootstrap.HostSkillInstalled] = {}
    try:
        _install_host_skills(panes, results)
    except Exception as exc:  # noqa: BLE001
        report(
            f"amux: could not install amux's skill ({exc}); agents will run without it"
        )
    return results


def _install_host_skills(
    panes: Sequence[PaneSpec],
    results: dict[str, sandbox_bootstrap.HostSkillInstalled],
) -> None:
    for spec in panes:
        if spec.agent not in AGENT_COMMANDS:
            continue
        result = results.get(spec.agent)
        if result is None:
            result = results[spec.agent] = sandbox_bootstrap.install_host_skill(
                spec.agent
            )
            if result.ok and result.changed:
                report(f"amux: installed amux's skill at {result.path}")
        if not result.ok:
            report(
                f"amux: {spec.name} has no amux skill installed "
                f"({result.reason}); it will not know amux's vocabulary"
            )


def host_skill_path(
    installed: dict[str, sandbox_bootstrap.HostSkillInstalled], agent: str
) -> str:
    result = installed.get(agent)
    return result.path if result is not None and result.ok else ""


@dataclass(frozen=True)
class Launch:
    pane: str
    cwd: str = ""
    keys: tuple[str, ...] = ()
    bootstrap: str = ""


class Runtime(Protocol):
    kind: str

    def preflight(
        self,
        agents: list[AgentRequest],
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

    def rollback(self) -> list[str]: ...


class HostRuntime:
    kind = HOST

    def preflight(
        self,
        agents: list[AgentRequest],
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
        installed = install_host_skills(panes)
        launches = []
        for spec in panes:
            command = render_command(
                AGENT_COMMANDS.get(spec.agent, spec.agent),
                (*render_tuning(spec.request), *skill_pointer_args(spec.agent)),
            )
            path = paths.get(spec.pane)
            keys = []
            if path:
                keys.append(worktree.shell_cd(path))
            if command:
                keys.append(command)
            launches.append(
                Launch(
                    pane=spec.pane,
                    cwd=path or cwd or "",
                    keys=tuple(keys),
                    bootstrap=skill_bootstrap_message(
                        spec.agent, host_skill_path(installed, spec.agent)
                    ),
                )
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
                [(spec.pane, spec.request, spec.name) for spec in panes],
            )
        except worktree.WorktreeError as exc:
            print(f"amux: worktree isolation unavailable: {exc}")
            return {}


@dataclass(frozen=True)
class AppleContainerConfig:
    image: str = apple_container.DEFAULT_IMAGE
    resources: sandbox.Resources = field(default_factory=sandbox.Resources)


class AppleContainerRuntime:
    """Host worktrees, containerized execution.

    Each agent keeps the normal per-agent worktree and branch; only the agent
    process is moved into an Apple `container` VM, with the repo and the
    worktree bind-mounted at their host paths. Nothing is acquired at prepare
    time -- the pane's shell creates the container by running the composed
    command -- so rollback has nothing to release, and integration and
    cleanup follow the host paths. The container has no amux client, so like
    a raw-command agent it emits no state events.
    """

    kind = APPLE_CONTAINER

    def __init__(self, config: AppleContainerConfig | None = None):
        self.config = config or AppleContainerConfig()

    def preflight(
        self,
        agents: list[AgentRequest],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None:
        repo = worktree.repo_root(cwd) if cwd else None
        apple_container.preflight(
            agents=[r.agent for r in agents],
            repo=repo or "",
            resources=self.config.resources,
            image=self.config.image,
        ).raise_if_failed(APPLE_CONTAINER, apple_container.ContainerError)

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
        if not (workspace and task and cwd):
            raise apple_container.ContainerError(
                "the apple-container runtime needs a workspace, task and path"
            )
        repo = worktree.repo_root(cwd)
        if not repo:
            raise apple_container.ContainerError(f"{cwd} is not a git repository")

        paths = worktree.setup_task(
            repo,
            workspace,
            task,
            [(spec.pane, spec.request, spec.name) for spec in panes],
        )
        # setup_task registered a host row per pane; flip each onto this
        # runtime so stop/clean know which container the pane owns.
        rows = store.worktrees_for_panes([spec.pane for spec in panes])

        launches = []
        for spec in panes:
            name = apple_container.container_name(workspace, task, spec.name, repo)
            store.set_worktree_runtime(
                rows[spec.pane]["id"],
                runtime=APPLE_CONTAINER,
                runtime_status="running",
                sandbox_name=name,
            )
            path = paths[spec.pane]
            command = apple_container.launch_command(
                name,
                image=self.config.image,
                resources=self.config.resources,
                repo=repo,
                workdir=path,
                request=spec.request,
            )
            script = apple_container.write_launch_script(
                name, workspace=workspace, task=task, command=command
            )
            launches.append(
                Launch(
                    pane=spec.pane,
                    cwd=path,
                    keys=(worktree.shell_cd(path), f"sh {shlex.quote(script)}"),
                )
            )
        return launches


def _context_service():
    from amux import context_service

    return context_service


GONE_RUNTIME_STATUSES = frozenset({"removed", "failed", "superseded"})


def _live_rows(kind: str, workspace: str, task: str | None) -> list[dict]:
    return [
        dict(row)
        for row in store.worktrees_for(workspace, task)
        if row["runtime"] == kind
        and row["sandbox_name"]
        and row["runtime_status"] not in GONE_RUNTIME_STATUSES
    ]


def sandbox_rows(workspace: str, task: str) -> list[dict]:
    return _live_rows(DOCKER_SANDBOX, workspace, task)


def _apple_rows(workspace: str, task: str) -> list[dict]:
    return _live_rows(APPLE_CONTAINER, workspace, task)


def sandbox_tasks(workspace: str) -> list[str]:
    seen: list[str] = []
    for kind in (DOCKER_SANDBOX, APPLE_CONTAINER):
        for row in _live_rows(kind, workspace, None):
            if row["task"] not in seen:
                seen.append(row["task"])
    return seen


def _retire(worktree_id: int, status: str, *, current: str) -> None:
    store.set_worktree_runtime(worktree_id, runtime_status=status)
    if current == "active":
        store.set_worktree_status(worktree_id, "removed")


def _existing_sandbox_names() -> set[str] | None:
    # None means "could not read the list": with no evidence a sandbox is
    # gone, the stops must still be attempted rather than skipped.
    try:
        return {str(entry.get("name") or "") for entry in sandbox.sandboxes()}
    except sandbox.SandboxError:
        return None


def stop_task(workspace: str, task: str) -> list[str]:
    stopped: list[str] = []
    docker_rows = sandbox_rows(workspace, task)
    known = _existing_sandbox_names() if docker_rows else None
    for row in docker_rows:
        name = row["sandbox_name"]
        if known is not None and name not in known:
            # Removed behind amux's back (`sbx rm -f`): reconcile once
            # instead of erroring on every stop pass forever.
            print(f"amux: {name} no longer exists; recording it as removed")
            store.set_worktree_runtime(row["id"], runtime_status="removed")
            continue
        try:
            sandbox.stop(name)
        except sandbox.SandboxError as exc:
            print(f"amux: could not stop sandbox {name}: {exc}")
            continue
        store.set_worktree_runtime(row["id"], runtime_status="stopped")
        stopped.append(name)
    # Apple containers run with --rm, so stopping one removes it (measured on
    # container 1.2.2); "removed" is the truthful record. The work is safe
    # either way: it lives in the mounted host worktree, not the container.
    # The launch script goes too: a "removed" row is invisible to clean_task,
    # so this is its last chance not to leak.
    for row in _apple_rows(workspace, task):
        name = row["sandbox_name"]
        try:
            apple_container.stop(name)
        except apple_container.ContainerError as exc:
            print(f"amux: could not stop container {name}: {exc}")
            continue
        apple_container.remove_launch_script(name)
        store.set_worktree_runtime(row["id"], runtime_status="removed")
        stopped.append(name)
    return stopped


def _clean_apple_task(workspace: str, task: str) -> list[str]:
    # No preservation pass and no --force distinction: the container mounts
    # the agent's host worktree, so committed and uncommitted work alike
    # already live on the host and worktree removal owns their fate.
    removed: list[str] = []
    problems: list[str] = []
    for row in _apple_rows(workspace, task):
        name = row["sandbox_name"]
        try:
            apple_container.remove(name)
        except apple_container.ContainerError as exc:
            problems.append(f"{name}: {exc}")
            continue
        apple_container.remove_launch_script(name)
        store.set_worktree_runtime(row["id"], runtime_status="removed")
        removed.append(name)
    if problems:
        raise sandbox.SandboxError(
            "some apple containers could not be removed:\n"
            + "\n".join(f"  {p}" for p in problems)
        )
    return removed


def clean_task(workspace: str, task: str, *, force: bool = False) -> list[str]:
    removed_apple = _clean_apple_task(workspace, task)
    by_sandbox: dict[str, list[dict]] = {}
    for row in sandbox_rows(workspace, task):
        by_sandbox.setdefault(row["sandbox_name"], []).append(row)
    if not by_sandbox:
        return removed_apple

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
    return removed_apple + removed


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
    resources: sandbox.Resources = field(default_factory=sandbox.Resources)
    port: int | None = None

    @property
    def resolved_port(self) -> int:
        return self.port if self.port is not None else _context_service().DEFAULT_PORT

    @property
    def policy_target(self) -> str:
        return f"localhost:{self.resolved_port}"

    @property
    def client_endpoint(self) -> str:
        return f"http://host.docker.internal:{self.resolved_port}"


@dataclass
class _Acquired:
    spec: PaneSpec
    sandbox_name: str
    repo: str = ""
    worktree_id: int | None = None
    token_id: int | None = None
    handle: sandbox.Sandbox | None = None
    hooks: sandbox_bootstrap.HooksInstalled | None = None
    reattached: bool = False


class SandboxRuntime:
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
        agents: list[AgentRequest],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None:
        repo = worktree.repo_root(cwd) if cwd else None
        sandbox.preflight(
            agents=[r.agent for r in agents],
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

        host_installed: dict[str, sandbox_bootstrap.HostSkillInstalled] = (
            install_host_skills(panes) if self.config.resources.share_skills else {}
        )

        launches = []
        for spec in panes:
            launches.append(
                self._create_one(
                    spec,
                    workspace=workspace,
                    task=task,
                    repo=repo,
                    socket=socket,
                    host_installed=host_installed,
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
        host_installed: dict[str, sandbox_bootstrap.HostSkillInstalled],
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
            model=spec.model,
            effort=spec.effort,
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

        if self.config.resources.share_skills:
            skill_path = (
                sandbox_bootstrap.sandbox_skill_destination(spec.agent, installed)
                if host_skill_path(host_installed, spec.agent)
                else ""
            )
        else:
            skill = sandbox_bootstrap.install_skill(handle, spec.agent, installed)
            skill_path = skill.path if skill.ok else ""
            if not skill.ok:
                report(
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
            cwd="",
            keys=(sandbox.attach_command(name, spec.agent, spec.request),),
            bootstrap=skill_bootstrap_message(spec.agent, skill_path),
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
            # Retire the runtime axis too: the VM lives on under the new
            # generation's row, and a prior row left "running"/"stopped"
            # makes every later stop/clean pass handle the same sandbox
            # once per generation.
            store.set_worktree_runtime(row["id"], runtime_status="superseded")
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
