"""Per-agent git worktrees with a per-task integration branch.

Layout (all outside the user's repo):

    $XDG_STATE_HOME/amux/worktrees/<workspace>/<task>/_integration/   -> amux/<ws>/<task>
    $XDG_STATE_HOME/amux/worktrees/<workspace>/<task>/<agent-name>/   -> amux/<ws>/<task>/<name>

Branch topology: agents branch off the task integration branch; `integrate`
merges them back into it. Merging the integration branch into the repo's main
line is left to the human.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from amux import store
from amux.shared import STATE_DIR

INTEGRATION_DIR = "_integration"


class WorktreeError(RuntimeError):
    pass


@dataclass
class MergeResult:
    pane: str
    name: str
    branch: str
    ok: bool
    commits: int = 0
    shortstat: str = ""
    error: str = ""


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return proc


def repo_root(path: str) -> str | None:
    """Absolute path of the repo containing `path`, else None."""
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def has_commits(repo: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0


def head_ref(repo: str) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def task_branch_namespace(workspace: str, task: str) -> str:
    return f"amux/{workspace}/{task}"


def integration_branch(workspace: str, task: str) -> str:
    # Leaf under the same namespace as agent branches (amux/<ws>/<task>/<name>).
    # A branch named amux/<ws>/<task> would collide with the refs directory.
    return f"{task_branch_namespace(workspace, task)}/integration"


def agent_branch(workspace: str, task: str, name: str) -> str:
    return f"{task_branch_namespace(workspace, task)}/{name}"


def task_worktree_root(workspace: str, task: str) -> str:
    return str(STATE_DIR / "worktrees" / workspace / task)


def _branch_exists(repo: str, branch: str) -> bool:
    return (
        _git(
            repo, "show-ref", "--verify", "-q", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    )


@dataclass(frozen=True)
class TaskIntegration:
    """The task's integration worktree, and the base every agent branches off.

    Both runtimes need this: host agents get worktrees branched from it, and
    sandboxed agents get clones whose committed branches are merged back into
    it. Only the per-agent step differs, so only the per-agent step is split.
    """

    repo: str
    workspace: str
    task: str
    base_ref: str
    branch: str
    path: str


def registered_worktrees(repo: str) -> set[str]:
    """Paths git currently considers worktrees of `repo`."""
    out = _git(repo, "worktree", "list", "--porcelain", check=False).stdout
    return {
        os.path.realpath(line.split(" ", 1)[1])
        for line in out.splitlines()
        if line.startswith("worktree ")
    }


def setup_task_integration(repo: str, workspace: str, task: str) -> TaskIntegration:
    """Create the task's integration branch and worktree, or adopt the existing
    one.

    Idempotent on purpose. `kg`/`kw` without `--clean` deliberately leave the
    integration worktree in place, so respawning that task must find it rather
    than fail on `worktree add` -- which is what made a resumed task
    unrecoverable without `--clean`.
    """
    if not has_commits(repo):
        raise WorktreeError("repo has no commits yet")
    base = head_ref(repo)
    branch = integration_branch(workspace, task)
    path = f"{task_worktree_root(workspace, task)}/{INTEGRATION_DIR}"

    if not _branch_exists(repo, branch):
        _git(repo, "branch", branch, base)
    if os.path.realpath(path) not in registered_worktrees(repo):
        _git(repo, "worktree", "add", path, branch)
    return TaskIntegration(
        repo=repo,
        workspace=workspace,
        task=task,
        base_ref=base,
        branch=branch,
        path=path,
    )


def remove_task_integration(integration: TaskIntegration) -> None:
    """Undo `setup_task_integration`.

    The branch is kept deliberately, as everywhere else in this module: it is
    the task's durable line, a retry is idempotent because `_branch_exists`
    short-circuits, and anything already merged into it must stay reachable.
    """
    _git(
        integration.repo,
        "worktree",
        "remove",
        "--force",
        integration.path,
        check=False,
    )


def setup_host_agents(
    integration: TaskIntegration,
    panes: list[tuple[str, str, str]],
) -> dict[str, str]:
    """One worktree + registry row per host pane, branched off the integration
    branch.

    `panes` is a list of (pane_id, agent, name). Returns {pane_id: path}. Rolls
    back its own worktrees and rows on failure; the integration worktree belongs
    to the caller that created it.
    """
    repo, workspace, task = integration.repo, integration.workspace, integration.task
    root = task_worktree_root(workspace, task)

    paths: dict[str, str] = {}
    # Tracked separately from `paths`: a worktree exists on disk from the moment
    # `worktree add` returns, but only joins `paths` once its row is registered.
    # Rolling back `paths` alone would strand the worktree of whichever pane the
    # registry failed on.
    created: list[str] = []
    registered: list[int] = []
    try:
        for pane_id, agent, name in panes:
            branch = agent_branch(workspace, task, name)
            path = f"{root}/{name}"
            _git(repo, "worktree", "add", path, "-b", branch, integration.branch)
            created.append(path)
            registered.append(
                store.register_worktree(
                    pane=pane_id,
                    workspace=workspace,
                    task=task,
                    agent=agent,
                    name=name,
                    path=path,
                    branch=branch,
                    base_ref=integration.base_ref,
                    repo=repo,
                )
            )
            paths[pane_id] = path
    except Exception:
        # Roll back this task's worktrees so a failed spawn leaves no debris.
        for path in created:
            _git(repo, "worktree", "remove", "--force", path, check=False)
        # The registry is append-only, so rows already inserted outlive the
        # rollback. Left active, a later integrate would merge branches whose
        # worktrees are gone.
        for wt_id in registered:
            store.set_worktree_status(wt_id, "removed")
        raise
    return paths


def setup_task(
    repo: str,
    workspace: str,
    task: str,
    panes: list[tuple[str, str, str]],
) -> dict[str, str]:
    """Host-runtime task setup: the integration worktree + one worktree per pane.

    A failure anywhere unwinds both halves, so a failed spawn leaves no debris.
    """
    integration = setup_task_integration(repo, workspace, task)
    try:
        return setup_host_agents(integration, panes)
    except Exception:
        remove_task_integration(integration)
        raise


def _merge_source(row: dict) -> str:
    """What `integrate` should merge for one execution row.

    A host row's branch is already local. A sandbox row's is not: it lives in
    the VM's clone and reaches the host only through the `sandbox-<name>`
    remote, so it is fetched to a durable ref first. Uncommitted files in the
    sandbox cannot cross that boundary, which is exactly the intended
    behaviour -- only committed work is integrated.
    """
    if row.get("runtime") != "docker-sandbox":
        return row["branch"]
    sandbox_name = row.get("sandbox_name") or ""
    if not sandbox_name:
        raise WorktreeError(
            f"sandbox row for '{row['name']}' has no sandbox name recorded; "
            "its branch cannot be located"
        )
    return fetch_sandbox_branch(row["repo"], sandbox_name, row["branch"])


def _record_failure(workspace: str, task: str, row: dict, text: str) -> None:
    """Leave a task-scoped blocker so a failed integrate is visible to the team
    rather than only to whoever happened to run the command."""
    store.add_note(
        workspace=workspace,
        task=task,
        pane=row["pane"],
        agent=row["agent"],
        worktree_id=row["id"],
        repo=row["repo"],
        scope="task",
        kind="blocker",
        text=text,
    )


def integrate(
    workspace: str,
    task: str,
    names: list[str] | None = None,
) -> list[MergeResult]:
    """Merge agent branches into the task integration branch.

    `names` limits the merge to those agent names; None means every active
    worktree of the task. Conflict aborts the merge and records a blocker note.
    """
    rows = [
        r
        for r in store.worktrees_for(workspace, task)
        if r["status"] == "active" and (names is None or r["name"] in names)
    ]
    if not rows:
        raise WorktreeError(
            f"no active worktrees for task '{task}' in workspace '{workspace}'"
        )
    int_path = f"{task_worktree_root(workspace, task)}/{INTEGRATION_DIR}"

    results: list[MergeResult] = []
    for row in rows:
        pane, name, branch = row["pane"], row["name"], row["branch"]
        wt_id, repo = row["id"], row["repo"]

        # A host agent's branch is already in this repository. A sandboxed
        # agent's lives inside its VM and reaches the host only through the
        # remote `sbx create --clone` published, so it must be fetched first --
        # and only committed work comes across, which is the point.
        try:
            source = _merge_source(row)
        except WorktreeError as exc:
            err = str(exc)
            _record_failure(
                workspace, task, row, f"integrate: cannot reach {name} ({branch}): {err}"
            )
            results.append(
                MergeResult(pane=pane, name=name, branch=branch, ok=False, error=err)
            )
            continue

        before = _git(int_path, "rev-parse", "HEAD").stdout.strip()
        n_commits = int(
            _git(int_path, "rev-list", "--count", f"HEAD..{source}").stdout.strip()
            or "0"
        )
        proc = _git(int_path, "merge", "--no-ff", source, check=False)
        if proc.returncode != 0:
            _git(int_path, "merge", "--abort", check=False)
            err = proc.stderr.strip() or proc.stdout.strip()
            _record_failure(
                workspace,
                task,
                row,
                f"integrate: conflict merging {name} ({branch}): {err}",
            )
            results.append(
                MergeResult(pane=pane, name=name, branch=branch, ok=False, error=err)
            )
            continue
        shortstat = _git(
            int_path, "diff", "--shortstat", before, "HEAD"
        ).stdout.strip()
        store.set_worktree_status(wt_id, "merged")
        store.add_note(
            workspace=workspace,
            task=task,
            pane=pane,
            agent=row["agent"],
            worktree_id=wt_id,
            repo=repo,
            scope="task",
            kind="note",
            text=(
                f"integrate: merged {name} ({branch}) — "
                f"{n_commits} commit(s), {shortstat or 'no changes'}"
            ),
        )
        results.append(
            MergeResult(
                pane=pane,
                name=name,
                branch=branch,
                ok=True,
                commits=n_commits,
                shortstat=shortstat,
            )
        )
    return results


def remove_task(workspace: str, task: str) -> list[str]:
    """Remove all worktrees of a task (branches are kept). Returns removed paths."""
    removed: list[str] = []
    rows = store.worktrees_for(workspace, task)
    if not rows:
        return removed
    int_path = f"{task_worktree_root(workspace, task)}/{INTEGRATION_DIR}"
    for row in rows:
        if row["status"] == "removed" or not row["repo"]:
            continue
        # A sandbox row has no host worktree (path=''), and `git worktree
        # remove ""` is not a no-op worth relying on. Sandboxes are removed by
        # `runtime.clean_task`, which has to check for uncommitted work first.
        if not row["path"]:
            continue
        # Per row: a task can span repos, and rows registered without one would
        # otherwise run `git -C ""` and fail silently under check=False.
        if _git(
            row["repo"], "worktree", "remove", "--force", row["path"], check=False
        ).returncode == 0:
            removed.append(row["path"])
            # Only on success — a row marked removed while its worktree is still
            # on disk is both unreachable and invisible.
            store.set_worktree_status(row["id"], "removed")
    for repo in {row["repo"] for row in rows if row["repo"]}:
        _git(repo, "worktree", "remove", "--force", int_path, check=False)
    return removed


def sandbox_remote(sandbox_name: str) -> str:
    """The host-side remote `sbx create --clone` publishes for a sandbox."""
    return f"sandbox-{sandbox_name}"


def sandbox_tracking_ref(sandbox_name: str, branch: str) -> str:
    """Where a fetched sandbox branch lands on the host.

    Namespaced under `refs/amux/` rather than `refs/heads/`: it is a record of
    what a sandbox had committed at fetch time, not a branch anyone checks out,
    and it must not collide with the identically named branch a *host* agent of
    the same name would own. It is also what survives `sbx rm`, which is why
    cleanup fetches before removing.
    """
    return f"refs/amux/sandboxes/{sandbox_name}/{branch}"


def fetch_sandbox_branch(repo: str, sandbox_name: str, branch: str) -> str:
    """Fetch a sandbox's committed branch to a durable local ref, and return it.

    Raises `WorktreeError` with git's own message when the sandbox is stopped,
    already removed, or has never committed the branch -- those are different
    problems and the caller reports them rather than papering over them.
    """
    remote = sandbox_remote(sandbox_name)
    ref = sandbox_tracking_ref(sandbox_name, branch)
    proc = _git(
        repo, "fetch", "--no-tags", remote, f"+{branch}:{ref}", check=False
    )
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return ref


def remove_sandbox_remote(repo: str, sandbox_name: str) -> None:
    """Drop a sandbox's host remote if it is still there.

    Unchecked: `sbx rm` may already have removed it, and `git remote remove`
    fails loudly on a remote that does not exist.
    """
    _git(repo, "remote", "remove", sandbox_remote(sandbox_name), check=False)


def latest_commit_subject(path: str) -> str:
    proc = _git(path, "log", "-1", "--format=%s", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def shell_cd(path: str) -> str:
    return f"cd {shlex.quote(path)}"
