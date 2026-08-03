"""Per-agent git worktrees with a per-task integration branch.

Layout (all outside the user's repo):

    $XDG_STATE_HOME/amux/worktrees/<workspace>/<task>/_integration/   -> amux/<ws>/<task>
    $XDG_STATE_HOME/amux/worktrees/<workspace>/<task>/<agent-name>/   -> amux/<ws>/<task>/<name>

Branch topology: agents branch off the task integration branch; `integrate`
merges them back into it. Merging the integration branch into the repo's main
line is left to the human.
"""

from __future__ import annotations

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


def setup_task(
    repo: str,
    workspace: str,
    task: str,
    panes: list[tuple[str, str, str]],
) -> dict[str, str]:
    """Create the integration worktree + one worktree per pane.

    `panes` is a list of (pane_id, agent, name). Returns {pane_id: worktree_path}.
    Registers every pane in the worktree store.
    """
    if not has_commits(repo):
        raise WorktreeError("repo has no commits yet")
    base = head_ref(repo)
    int_branch = integration_branch(workspace, task)
    root = task_worktree_root(workspace, task)
    int_path = f"{root}/{INTEGRATION_DIR}"

    if not _branch_exists(repo, int_branch):
        _git(repo, "branch", int_branch, base)
    _git(repo, "worktree", "add", int_path, int_branch)

    paths: dict[str, str] = {}
    registered: list[int] = []
    try:
        for pane_id, agent, name in panes:
            branch = agent_branch(workspace, task, name)
            path = f"{root}/{name}"
            _git(repo, "worktree", "add", path, "-b", branch, int_branch)
            registered.append(
                store.register_worktree(
                    pane=pane_id,
                    workspace=workspace,
                    task=task,
                    agent=agent,
                    name=name,
                    path=path,
                    branch=branch,
                    base_ref=base,
                    repo=repo,
                )
            )
            paths[pane_id] = path
    except Exception:
        # Roll back this task's worktrees so a failed spawn leaves no debris.
        for path in [int_path, *paths.values()]:
            _git(repo, "worktree", "remove", "--force", path, check=False)
        # The registry is append-only, so rows already inserted outlive the
        # rollback. Left active, a later integrate would merge branches whose
        # worktrees are gone.
        for wt_id in registered:
            store.set_worktree_status(wt_id, "removed")
        raise
    return paths


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
        before = _git(int_path, "rev-parse", "HEAD").stdout.strip()
        n_commits = int(
            _git(int_path, "rev-list", "--count", f"HEAD..{branch}").stdout.strip()
            or "0"
        )
        proc = _git(int_path, "merge", "--no-ff", branch, check=False)
        if proc.returncode != 0:
            _git(int_path, "merge", "--abort", check=False)
            err = proc.stderr.strip() or proc.stdout.strip()
            store.add_note(
                workspace=workspace,
                task=task,
                pane=pane,
                agent=row["agent"],
                worktree_id=wt_id,
                repo=repo,
                scope="task",
                kind="blocker",
                text=f"integrate: conflict merging {name} ({branch}): {err}",
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


def latest_commit_subject(path: str) -> str:
    proc = _git(path, "log", "-1", "--format=%s", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def shell_cd(path: str) -> str:
    return f"cd {shlex.quote(path)}"
