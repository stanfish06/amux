from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from amux.sandbox import Check, Preflight, Resources, SandboxError, sandbox_name
from amux.shared import STATE_DIR, AgentRequest, render_command, render_tuning

CONTAINER = "container"

DEFAULT_IMAGE = "docker.io/library/node:22"

DEFAULT_TIMEOUT_S = 120.0

SUPPORTED_AGENTS = ("claude", "codex")

AGENT_ARGV: dict[str, tuple[str, ...]] = {
    "claude": (
        "npx",
        "-y",
        "@anthropic-ai/claude-code",
        "--dangerously-skip-permissions",
    ),
    "codex": (
        "npx",
        "-y",
        "@openai/codex",
        "--dangerously-bypass-approvals-and-sandbox",
    ),
}

AGENT_ENV: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "IS_SANDBOX=1"),
    "codex": ("OPENAI_API_KEY",),
}

_GIT_SAFE_ENV = (
    "GIT_CONFIG_COUNT=1",
    "GIT_CONFIG_KEY_0=safe.directory",
    "GIT_CONFIG_VALUE_0=*",
)


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        text = self.stderr.strip() or self.stdout.strip()
        return (
            text.splitlines()[0].strip()
            if text
            else f"container exited {self.returncode}"
        )


def run(
    *args: str,
    check: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> ContainerResult:
    argv = (CONTAINER, *args)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ContainerError(
            "Apple's `container` CLI is not installed or not on PATH; install "
            "it (brew install container) and re-run, or spawn without "
            "--runtime apple-container"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ContainerError(
            f"container {' '.join(args)} timed out after {timeout:g}s"
        ) from exc

    result = ContainerResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and not result.ok:
        raise ContainerError(f"container {' '.join(args)}: {result.message}")
    return result


def version() -> str:
    return run("--version").stdout.strip()


def system_running() -> tuple[bool, str]:
    result = run("system", "status", check=False)
    return result.ok, "running" if result.ok else result.message


container_name = sandbox_name


def _missing(result: ContainerResult) -> bool:
    return "notFound" in result.stderr


def stop(name: str) -> None:
    result = run("stop", name, check=False)
    if not result.ok and not _missing(result):
        raise ContainerError(f"container stop {name}: {result.message}")


def remove(name: str) -> None:
    # Always forced: the work lives in the host worktree, not the container.
    result = run("delete", "--force", name, check=False)
    if not result.ok and not _missing(result):
        raise ContainerError(f"container delete {name}: {result.message}")


def git_identity_env(repo: str) -> tuple[str, ...]:
    pairs = []
    for key, env_keys in (
        ("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
        ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")),
    ):
        proc = subprocess.run(
            ["git", "-C", repo, "config", key], capture_output=True, text=True
        )
        value = proc.stdout.strip()
        if proc.returncode == 0 and value:
            pairs += [f"{env_key}={value}" for env_key in env_keys]
    return tuple(pairs)


def launch_argv(
    name: str,
    *,
    image: str,
    resources: Resources,
    repo: str,
    workdir: str,
    request: AgentRequest,
) -> tuple[str, ...]:
    agent = request.agent
    agent_argv = AGENT_ARGV.get(agent)
    if agent_argv is None:
        raise ContainerError(
            f"agent '{agent}' is not supported by the apple-container runtime; "
            f"supported: {', '.join(SUPPORTED_AGENTS)}"
        )
    args = [
        "run",
        "--interactive",
        "--tty",
        "--rm",
        "--name",
        name,
        "--cpus",
        str(resources.cpus),
        "--memory",
        resources.memory,
        "--volume",
        f"{repo}:{repo}",
        "--volume",
        f"{workdir}:{workdir}",
        "--workdir",
        workdir,
    ]
    for env in (*_GIT_SAFE_ENV, *git_identity_env(repo), *AGENT_ENV.get(agent, ())):
        args += ["--env", env]
    args.append(image)
    args += [*agent_argv, *render_tuning(request)]
    return tuple(args)


def launch_command(
    name: str,
    *,
    image: str,
    resources: Resources,
    repo: str,
    workdir: str,
    request: AgentRequest,
) -> str:
    return render_command(
        CONTAINER,
        launch_argv(
            name,
            image=image,
            resources=resources,
            repo=repo,
            workdir=workdir,
            request=request,
        ),
    )


def launch_script_path(name: str) -> str:
    return str(STATE_DIR / "launch" / f"{name}.sh")


def write_launch_script(name: str, *, workspace: str, task: str, command: str) -> str:
    path = Path(launch_script_path(name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!/bin/sh\n# amux {workspace}/{task}: agent container {name}\n"
        f"exec {command}\n"
    )
    path.chmod(0o700)
    return str(path)


def remove_launch_script(name: str) -> None:
    Path(launch_script_path(name)).unlink(missing_ok=True)


def preflight(
    *,
    agents: Sequence[str],
    repo: str,
    resources: Resources,
    image: str,
) -> Preflight:
    checks: list[Check] = []

    unsupported = sorted({a for a in agents if a not in SUPPORTED_AGENTS})
    checks.append(
        Check(
            "agents",
            not unsupported,
            ", ".join(agents)
            if not unsupported
            else f"unsupported: {', '.join(unsupported)}",
            f"the apple-container runtime supports "
            f"{' and '.join(SUPPORTED_AGENTS)}; "
            "spawn the others with the default host runtime",
        )
    )

    if not repo:
        checks.append(
            Check(
                "repository",
                False,
                "no git repository",
                "spawn inside a git repository; the runtime mounts per-agent "
                "worktrees, which need one",
            )
        )
    else:
        checks.append(Check("repository", True, repo))

    try:
        resources.validate()
        checks.append(
            Check("resources", True, f"{resources.cpus} cpu, {resources.memory}")
        )
    except SandboxError as exc:
        checks.append(Check("resources", False, str(exc), "correct the resource flags"))

    checks.append(Check("image", True, image))

    try:
        detected = version()
    except ContainerError as exc:
        checks.append(
            Check(
                "container",
                False,
                str(exc),
                "brew install container",
            )
        )
        return Preflight(tuple(checks))
    checks.append(Check("container", True, detected))

    running, detail = system_running()
    checks.append(
        Check(
            "container-system",
            running,
            detail,
            "run `container system start` (and install a default kernel with "
            "`container system kernel set --recommended` if it asks for one)",
        )
    )
    return Preflight(tuple(checks))
