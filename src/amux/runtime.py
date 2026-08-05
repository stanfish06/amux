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
    """A grid failed partway through creation and was unwound.

    Carries the originating failure and every cleanup failure alongside it.
    The original cause is never replaced: a rollback runs while an error is
    already propagating, and a secondary "could not remove sandbox" would
    otherwise be the only thing the user sees.
    """

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

    def preflight(
        self,
        agents: list[str],
        *,
        workspace: str | None,
        task: str | None,
        cwd: str | None,
    ) -> None:
        """Reject an impossible grid *before* anything is created.

        Called before the session or window exists, because a runtime whose
        prerequisites are missing must leave no tmux session, sandbox, git
        reference or registry row behind. Raises to refuse.
        """
        ...

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
    ) -> dict[str, list[str]]:
        """Prior agent names this runtime could resume, keyed by agent kind.

        `core` assigns pane names, and a resumable execution can only be found
        by its name -- so if nothing steers a pane onto a prior name, resume
        code is unreachable however correct it is. Returning {} means "always
        draw a fresh name".
        """
        ...

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
    ) -> None:
        """Nothing to check: the host runtime has no external prerequisites,
        accepts any raw command as an agent, and already degrades to a shared
        directory when the target is not a repository."""

    def resumable_names(
        self, *, workspace: str | None, task: str | None, cwd: str | None
    ) -> dict[str, list[str]]:
        """Nothing. A host agent's worktree is gone once its task was cleaned,
        and if it was not, its branch and directory still exist -- so reusing
        the name would fail `worktree add` rather than resume anything. There is
        no host equivalent of a stopped VM waiting to be re-entered."""
        return {}

    def rollback(self) -> list[str]:
        """Nothing to unwind here. `setup_task` already rolls itself back, and
        it is the only durable resource the host runtime acquires."""
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
        """Per-agent git worktrees when the target dir is a repo. Fail soft: a
        non-repo target keeps today's shared-directory behavior.

        A missing `cwd` is a different thing from a non-repo one and used to
        share this branch silently: a non-repo target is a deliberate choice,
        while an unresolved directory means nobody worked out where the grid
        lives. That combination -- a known workspace and task but no path --
        skipped per-agent worktrees for every `spg` without `-p`, and tmux
        inheritance left the panes in a shared directory so it looked correct.
        It is still fail-soft, because `cwd` is part of a public signature and
        callers may legitimately pass None, but it no longer does so quietly.
        """
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
    """Imported late: `context_service` imports `core`, which imports this
    module, so a top-level import would be a cycle."""
    from amux import context_service

    return context_service


#: `runtime_status` values meaning the VM is already gone. Anything else on a
#: row with a sandbox name may still be a live microVM.
GONE_RUNTIME_STATUSES = frozenset({"removed", "failed"})


def sandbox_rows(workspace: str, task: str) -> list[dict]:
    """Every row of a task that may still have a microVM behind it.

    Selected on the *runtime* axis, never the merge axis. `status` answers "was
    this work merged"; `runtime_status` answers "does a VM exist". Cleanup and
    stop need the second, and asking the first is how an integrated task leaked
    every sandbox it had: `integrate` sets status='merged', so a filter on
    'active' skipped exactly the rows whose VMs were still running.

    Deliberately permissive: anything with a sandbox name that is not already
    recorded gone is included. Acting on a VM that turns out to be absent is
    reported and harmless; skipping one that exists is the bug.
    """
    return [
        dict(row)
        for row in store.worktrees_for(workspace, task)
        if row["runtime"] == DOCKER_SANDBOX
        and row["sandbox_name"]
        and row["runtime_status"] not in GONE_RUNTIME_STATUSES
    ]


def sandbox_tasks(workspace: str) -> list[str]:
    """Tasks of a workspace that still have a microVM, read from the registry.

    The registry, not live tmux windows, because `kg` without `--clean`
    deliberately removes the window and keeps the VM -- after which the task
    exists only here. A caller enumerating windows cannot reach it at all, so
    such a task was orphaned by construction.

    Same lesson as `_taken_names` and `pane_states` before it: enumerate the
    durable record, not the live view.
    """
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
    """Record that a row's VM is gone, without rewriting its merge history.

    `runtime_status` always moves; `status` only when the execution was still
    active. A merged row stays merged -- the work really was merged, and only
    the VM went away.
    """
    store.set_worktree_runtime(worktree_id, runtime_status=status)
    if current == "active":
        store.set_worktree_status(worktree_id, "removed")


def stop_task(workspace: str, task: str) -> list[str]:
    """Stop every sandbox backing a task without destroying anything.

    This is `kg`/`kw` *without* `--clean`: the microVM keeps its disk, its
    working tree and whatever provider session the agent signed into, and its
    capability is deliberately NOT revoked -- a stopped sandbox must be able to
    resume as itself rather than be rebuilt. Cleanup is a separate, explicit
    act.

    Failures are reported and skipped rather than raised: killing a task must
    not be blocked by one stubborn sandbox.
    """
    stopped: list[str] = []
    for row in sandbox_rows(workspace, task):
        name = row["sandbox_name"]
        try:
            sandbox.stop(name)
        except sandbox.SandboxError as exc:
            print(f"amux: could not stop sandbox {name}: {exc}")
            continue
        # Only after a successful stop: a row marked stopped while its VM is
        # still running would misreport the installation.
        store.set_worktree_runtime(row["id"], runtime_status="stopped")
        stopped.append(name)
    return stopped


def clean_task(workspace: str, task: str, *, force: bool = False) -> list[str]:
    """Remove a task's sandboxes, preserving committed work first.

    Iterates distinct SANDBOXES, not rows. Reattachment leaves several rows
    naming one sandbox -- the dead pane's and the live one's -- and iterating
    rows removed that VM once and then tried to wake it again for the next row,
    reporting a survivor it had already deleted. Worse, that refusal aborted the
    remaining tasks, so a single stale row made a whole workspace permanently
    un-cleanable: worse than the leak it replaced.

    The order per sandbox is the safety property: confirm it still exists, wake
    it, establish whether it has a committed tip, preserve that tip, remove the
    sandbox, drop its remote, revoke its capabilities, and only then retire its
    rows. Refusals are raised, not printed, because the caller kills the tmux
    session afterwards and a session killed over surviving VMs leaves them
    unaddressable.
    """
    by_sandbox: dict[str, list[dict]] = {}
    for row in sandbox_rows(workspace, task):
        by_sandbox.setdefault(row["sandbox_name"], []).append(row)
    if not by_sandbox:
        return []

    # A sandbox amux has a row for but `sbx` does not know about is already
    # gone -- removed by hand, or by an earlier pass that got part way. There is
    # nothing to preserve and nothing to remove, so it is retired rather than
    # reported as a survivor. Reporting it stranded is what made re-running
    # `kw` fail identically forever.
    gone = {name for name in by_sandbox if not sandbox.exists(name)}
    for name in gone:
        print(f"amux: {name} no longer exists; recording it as removed")
        _retire_all(by_sandbox.pop(name))

    live = list(by_sandbox.items())
    if not force:
        dirty = [
            (name, status) for name, _ in live if (status := _dirty_status(name))
        ]
        if dirty:
            raise sandbox.SandboxError(_dirty_refusal(dirty))

    removed: list[str] = []
    stranded: list[str] = []
    for name, rows in live:
        branch = rows[0]["branch"]
        repo = rows[0]["repo"]
        handle = sandbox.Sandbox(name=name)
        # Restored on every refusal below, not just one of them: inspecting a
        # sandbox starts it, so any path that gives up after this point would
        # otherwise leave a VM running that the registry still calls stopped.
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

        # Re-resolved AFTER waking, never taken from the row: `sbx stop` tears
        # down both the published port and the host-side `sandbox-<name>`
        # remote, and waking republishes on a DIFFERENT host port without
        # restoring the remote. So a sandbox that has ever been stopped has no
        # remote to fetch by name, while its commits sit perfectly reachable at
        # the new port.
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
            # Unreachable, so whether it holds commits is unknown. `--force`
            # authorises losing *uncommitted* work; it does not authorise
            # destroying commits nobody has managed to copy out.
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
    """Retire every row naming one sandbox, and revoke every capability.

    All of them, because reattachment leaves the dead pane's row alongside the
    live one and a token left valid on a retired row still authenticates.
    """
    for row in rows:
        store.revoke_context_tokens_for_worktree(row["id"])
        _retire(row["id"], "removed", current=row["status"])


def _restore_stopped(name: str) -> None:
    """Put a sandbox back to stopped, having woken it to inspect it.

    Called from every refusal path, not just the `sbx rm` one: a VM left running
    while the registry still calls it stopped burns memory and disagrees with
    its own record, and the fetch-failure path reaches that state too.
    """
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
    """Uncommitted changes in a sandbox, or "" when it is clean.

    A sandbox that cannot be asked counts as dirty. Treating an unanswerable
    question as "clean" would delete work on exactly the sandboxes that are
    already misbehaving.
    """
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
    # None -> the service's default, resolved at call time. A dataclass default
    # is captured when the class is defined, which is precisely how a config
    # ends up pinned to a value a test can never patch.
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
    """One sandbox's resources, in the order they were acquired.

    Rollback walks this in reverse. Recorded as it goes rather than inferred
    afterwards, because a failure halfway through leaves a state no amount of
    inspection can reconstruct -- a sandbox may exist without a row, or a row
    without a token.
    """

    spec: PaneSpec
    sandbox_name: str
    repo: str = ""
    worktree_id: int | None = None
    token_id: int | None = None
    handle: sandbox.Sandbox | None = None
    hooks: sandbox_bootstrap.HooksInstalled | None = None
    #: True when this pane resumed an existing sandbox rather than creating one.
    #: Rollback must not destroy a VM this run did not build.
    reattached: bool = False


class SandboxRuntime:
    """Agents run inside per-agent Docker Sandbox microVMs.

    Each agent gets its own clone-mode sandbox, its own branch off the task
    integration line, its own capability token, and a pane that attaches with
    `sbx run --name`. amux keeps coordination on the host: the sandbox never
    sees the state directory, the database, or the tmux socket.
    """

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
        #: Per-pane hook installation results, keyed by pane id. What 5.4 reads
        #: to render `state_degraded` and `missing_kinds`: an agent whose hooks
        #: could not be fully installed cannot report every state, and its
        #: resolved state must not be presented as authoritative.
        self.hooks: dict[str, sandbox_bootstrap.HooksInstalled] = {}

    # --- preflight ---

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
        """Prior agent names for this task whose microVM may still exist.

        Keyed by agent kind on purpose: a sandbox holds the agent it was built
        for, so handing a `codex` pane the name of a stopped `claude` sandbox
        would reattach it to the wrong tool. Filtered by repository for the same
        reason a sandbox name carries a repo digest -- workspace and task are
        reusable labels and two checkouts must not adopt each other's VMs.

        Oldest first, so a respawned grid lands on its prior names in roughly
        the order it created them.
        """
        if not (workspace and task):
            return {}
        repo = worktree.repo_root(cwd) if cwd else None
        by_agent: dict[str, list[str]] = {}
        for row in sorted(
            sandbox_rows(workspace, task), key=lambda r: r["created_ts"]
        ):
            if repo and row["repo"] != repo:
                continue
            if not row["name"]:
                continue
            by_agent.setdefault(row["agent"], []).append(row["name"])
        return by_agent

    # --- creation ---

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

        # Shared with the host runtime: sandboxed branches are merged back into
        # this same integration line.
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

        # Reattachment is decided from amux's own record, not from the name.
        # A prior row is what says "this agent already had a sandbox"; `sbx ls`
        # only confirms the VM is still there. Deriving it from the name alone
        # would adopt any sandbox that happened to match, and would be unable
        # to tell "stopped" from "the user removed it behind our back".
        prior = self._prior_row(workspace, task, spec.name)
        existing = sandbox.find(name) if prior else None
        if existing is not None:
            handle = sandbox.Sandbox(
                name=name, id=str(existing.get("id") or ""), entry=existing
            )
            acquired.reattached = True
        else:
            if prior:
                # Recorded but gone: the sandbox was removed outside amux. Say
                # so rather than silently building a replacement that has none
                # of the agent's previous work in it.
                print(
                    f"amux: sandbox {name} was recorded but no longer exists; "
                    "creating a new one (its previous contents are not recoverable)"
                )
            handle = sandbox.create(name, spec.agent, repo, self.config.resources)
        acquired.handle = handle

        # A prior row belongs to a pane that no longer exists. Supersede it
        # rather than leaving two active rows for one sandbox, which would make
        # `integrate` merge the same branch twice and leave a stale capability
        # valid. This applies whether the VM was resumed or rebuilt.
        if prior:
            self._supersede(workspace, task, name)

        # The row is the durable identity notes and events hang off, so it
        # exists before anything is delivered into the sandbox. `path` is empty
        # because a sandbox has no host worktree; `socket_name` is explicit
        # because an empty one now means "pre-schema-3 row".
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

        # The agent works on its own named branch, cut from the task base so
        # `integrate` has a common ancestor to merge from. On a resumed sandbox
        # the branch is already there with the agent's commits on it, so it is
        # checked out rather than created -- `-b` would fail and, worse, would
        # be the wrong request.
        if acquired.reattached:
            handle.exec(["git", "checkout", branch])
        else:
            handle.exec(["git", "checkout", "-b", branch])

        # The service's own vocabulary, never a hand-rolled list: a drift
        # between what is minted and what the routes require surfaces as a 403
        # that reads like an auth bug.
        plaintext, token_id = store.mint_context_token(
            acquired.worktree_id, permissions=_context_service().AGENT_PERMISSIONS
        )
        acquired.token_id = token_id
        installed = sandbox_bootstrap.install_client(
            handle, endpoint=self.config.client_endpoint, token=plaintext
        )

        # The shim and the capability alone give the agent a working `amux` that
        # never reports anything: state events come from the agent's own hooks.
        # Without this the sandbox reads permanently idle, which no offline test
        # can catch because hooks only fire inside a live VM.
        hooks = sandbox_bootstrap.install_hooks(handle, spec.agent, installed)
        acquired.hooks = hooks
        self.hooks[spec.pane] = hooks
        if hooks.degraded:
            # Surfaced, not swallowed: a degraded agent cannot report every
            # state, and presenting its resolved state as authoritative would
            # claim an accuracy amux does not have.
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
        """The active sandbox row a previous pane left for this agent, if any.

        This is amux's record that the agent already has a VM. `kg` without
        `--clean` leaves exactly this behind, which is what makes a later
        spawn a resume rather than a rebuild.
        """
        for row in sandbox_rows(workspace, task):
            if row["name"] == agent_name:
                return row
        return None

    @staticmethod
    def _supersede(workspace: str, task: str, sandbox_name: str) -> None:
        """Retire the rows a previous pane left behind for this sandbox.

        The registry is append-only, so the old row is marked rather than
        deleted -- notes and events already point at it and must stay
        resolvable. Its capability is revoked because the new pane mints its
        own, and a token whose pane is gone should not keep working.
        """
        for row in sandbox_rows(workspace, task):
            if row["sandbox_name"] != sandbox_name:
                continue
            # The capability goes regardless -- the new pane mints its own, and
            # a dead pane's token should not keep working. The merge record
            # only moves if this execution was still active.
            store.revoke_context_tokens_for_worktree(row["id"])
            if row["status"] == "active":
                store.set_worktree_status(row["id"], "removed")

    # --- rollback ---

    def rollback(self) -> list[str]:
        """Release everything `prepare` acquired, newest resource first.

        Returns the failures rather than raising them: a rollback runs while an
        original error is already propagating, and losing that error to a
        secondary cleanup failure would hide the real cause.
        """
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
            # Only a sandbox this run created. Destroying one we merely resumed
            # would turn a failed respawn into data loss for work that predates
            # this command entirely.
            try:
                sandbox.remove(acquired.sandbox_name, force=True)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{acquired.sandbox_name}: remove sandbox: {exc}")
            # `sbx create --clone` publishes a host-side `sandbox-<name>` remote.
            # Removing it is best-effort and unchecked: sbx may already have
            # taken it with the sandbox, and a stale remote is worth reporting
            # but never worth failing a rollback over.
            if acquired.repo:
                try:
                    worktree.remove_sandbox_remote(
                        acquired.repo, acquired.sandbox_name
                    )
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{acquired.sandbox_name}: remove remote: {exc}")
        if acquired.worktree_id is not None:
            # Marked rather than deleted: the registry is append-only, and a row
            # left active would let a later integrate merge a branch whose
            # sandbox is gone. Both fields are set because they answer different
            # questions -- `runtime_status` why the VM is gone, `status` that
            # amux should stop considering this execution.
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
