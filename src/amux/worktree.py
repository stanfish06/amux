from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from amux import store
from amux.shared import STATE_DIR, AgentRequest

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
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return proc


def repo_root(path: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def has_commits(repo: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    )


def head_ref(repo: str) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def task_branch_namespace(workspace: str, task: str) -> str:
    return f"amux/{workspace}/{task}"


def integration_branch(workspace: str, task: str) -> str:
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
    repo: str
    workspace: str
    task: str
    base_ref: str
    branch: str
    path: str


def registered_worktrees(repo: str) -> set[str]:
    out = _git(repo, "worktree", "list", "--porcelain", check=False).stdout
    return {
        os.path.realpath(line.split(" ", 1)[1])
        for line in out.splitlines()
        if line.startswith("worktree ")
    }


def setup_task_integration(repo: str, workspace: str, task: str) -> TaskIntegration:
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
    panes: list[tuple[str, AgentRequest, str]],
) -> dict[str, str]:
    repo, workspace, task = integration.repo, integration.workspace, integration.task
    root = task_worktree_root(workspace, task)

    paths: dict[str, str] = {}
    created: list[str] = []
    registered: list[int] = []
    try:
        for pane_id, request, name in panes:
            branch = agent_branch(workspace, task, name)
            path = f"{root}/{name}"
            _git(repo, "worktree", "add", path, "-b", branch, integration.branch)
            created.append(path)
            registered.append(
                store.register_worktree(
                    pane=pane_id,
                    workspace=workspace,
                    task=task,
                    agent=request.agent,
                    name=name,
                    path=path,
                    branch=branch,
                    base_ref=integration.base_ref,
                    repo=repo,
                    model=request.model,
                    effort=request.effort,
                )
            )
            paths[pane_id] = path
    except Exception:
        for path in created:
            _git(repo, "worktree", "remove", "--force", path, check=False)
        for wt_id in registered:
            store.set_worktree_status(wt_id, "removed")
        raise
    return paths


def setup_task(
    repo: str,
    workspace: str,
    task: str,
    panes: list[tuple[str, AgentRequest, str]],
) -> dict[str, str]:
    integration = setup_task_integration(repo, workspace, task)
    try:
        return setup_host_agents(integration, panes)
    except Exception:
        remove_task_integration(integration)
        raise


def _merge_source(row: dict) -> str:
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

        try:
            source = _merge_source(row)
        except WorktreeError as exc:
            err = str(exc)
            _record_failure(
                workspace,
                task,
                row,
                f"integrate: cannot reach {name} ({branch}): {err}",
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
        shortstat = _git(int_path, "diff", "--shortstat", before, "HEAD").stdout.strip()
        if n_commits:
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
    removed: list[str] = []
    rows = store.worktrees_for(workspace, task)
    if not rows:
        return removed
    int_path = f"{task_worktree_root(workspace, task)}/{INTEGRATION_DIR}"
    for row in rows:
        if row["status"] == "removed" or not row["repo"]:
            continue
        if not row["path"]:
            continue
        if (
            _git(
                row["repo"], "worktree", "remove", "--force", row["path"], check=False
            ).returncode
            == 0
        ):
            removed.append(row["path"])
            store.set_worktree_status(row["id"], "removed")
    for repo in {row["repo"] for row in rows if row["repo"]}:
        _git(repo, "worktree", "remove", "--force", int_path, check=False)
    return removed


def sandbox_remote(sandbox_name: str) -> str:
    return f"sandbox-{sandbox_name}"


def sandbox_tracking_ref(sandbox_name: str, branch: str) -> str:
    return f"refs/amux/sandboxes/{sandbox_name}/{branch}"


def sandbox_branch_tip(
    repo: str, sandbox_name: str, branch: str, *, source: str | None = None
) -> str | None:
    proc = _git(
        repo, "ls-remote", source or sandbox_remote(sandbox_name), branch, check=False
    )
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    out = proc.stdout.strip()
    return out.split()[0] if out else None


def fetch_sandbox_branch(
    repo: str, sandbox_name: str, branch: str, *, source: str | None = None
) -> str:
    ref = sandbox_tracking_ref(sandbox_name, branch)
    proc = _git(
        repo,
        "fetch",
        "--no-tags",
        source or sandbox_remote(sandbox_name),
        f"+{branch}:{ref}",
        check=False,
    )
    if proc.returncode != 0:
        raise WorktreeError(proc.stderr.strip() or proc.stdout.strip())
    return ref


def remove_sandbox_remote(repo: str, sandbox_name: str) -> None:
    _git(repo, "remote", "remove", sandbox_remote(sandbox_name), check=False)


def latest_commit_subject(path: str) -> str:
    if not path:
        return ""
    proc = _git(path, "log", "-1", "--format=%s", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def shell_cd(path: str) -> str:
    return f"cd {shlex.quote(path)}"
