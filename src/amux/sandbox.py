"""Docker Sandbox (`sbx`) adapter.

Every `sbx` invocation amux makes goes through this module, and nothing outside
it parses `sbx` output. `sbx` is an evolving external tool: isolating it here
means a CLI change is a change to one file with one set of fixtures, rather
than a hunt through orchestration code.

The command surface below was verified against **sbx v0.37.1** by running the
real CLI, not read from a design document. Where the two disagreed, the CLI
won. In particular:

- there is no `sbx inspect`; the only machine-readable listing is
  `sbx ls --json`, which returns `{"sandboxes": [...]}`;
- there is no `--version` flag; `sbx version` prints
  `sbx version: v0.37.1 <sha>` on stdout;
- `sbx diagnose -o json` returns `{"version", "checks": [...], "summary"}`
  where each check carries `name`/`status`/`message`/`detail`/`hint` and
  status is one of pass/warn/fail/skip;
- `sbx policy check network HOST:PORT` is read-only and is how reachability is
  tested without mutating policy. It exits non-zero with a `412` and a
  `sbx policy init` hint while the global policy is uninitialized;
- `--no-share-skills` is absent from `sbx create --help` but is accepted;
- sandbox names admit only letters, numbers, hyphens, periods and plus signs.

Nothing here mutates Docker policy or installs anything. `sbx policy init` in
particular is a global, one-time, user-owned decision: amux detects that it is
missing and reports the exact command, and never runs it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SBX = "sbx"

# The surface documented above was verified against this release. Older builds
# predate `sbx ls --json`, so the adapter reports rather than guesses.
MIN_VERSION = (0, 37, 0)

# Docker's supported agent kinds are broader than this; the prototype commits
# only to the two amux already launches on the host.
SUPPORTED_AGENTS = ("claude", "codex")

DEFAULT_TIMEOUT_S = 120.0

# `sbx create --name` accepts letters, numbers, hyphens, periods and plus signs.
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9.+-]+")
# `-m/--memory` takes binary units, e.g. `1024m`, `8g`.
_MEMORY_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[kmgt]i?b?$", re.IGNORECASE)

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class SandboxError(RuntimeError):
    """An `sbx` invocation failed, or its output could not be trusted."""


@dataclass(frozen=True)
class SbxResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def message(self) -> str:
        """The most useful line `sbx` produced, for surfacing to a user."""
        text = self.stderr.strip() or self.stdout.strip()
        return text.splitlines()[0].strip() if text else f"sbx exited {self.returncode}"


def run(
    *args: str,
    check: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
    cwd: str | os.PathLike[str] | None = None,
) -> SbxResult:
    """Invoke `sbx` and capture both streams.

    Arguments must never carry secrets: argv is visible to every process on the
    host. Tokens reach a sandbox as a file via `copy_in`, never as a flag.
    """
    argv = (SBX, *args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise SandboxError(
            "sbx is not installed or not on PATH; install Docker Sandboxes "
            "and re-run, or spawn without --runtime docker-sandbox"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"sbx {' '.join(args)} timed out after {timeout:g}s") from exc

    result = SbxResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and not result.ok:
        raise SandboxError(f"sbx {' '.join(args)}: {result.message}")
    return result


def _json(result: SbxResult, what: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError(
            f"could not parse {what} from sbx: {result.stdout.strip()[:200]!r}"
        ) from exc


# --- version ---


def version() -> str:
    """The installed `sbx` version, e.g. `v0.37.1`.

    `sbx version` prints `sbx version: <version> <commit>`; only the version is
    stable enough to act on.
    """
    out = run("version").stdout.strip()
    match = _VERSION_RE.search(out)
    if not match:
        raise SandboxError(f"could not read an sbx version from {out!r}")
    return f"v{match.group(1)}.{match.group(2)}.{match.group(3)}"


def version_tuple(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.search(text)
    if not match:
        raise SandboxError(f"could not read an sbx version from {text!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_supported(text: str) -> bool:
    return version_tuple(text) >= MIN_VERSION


def unsupported_reason(text: str) -> str:
    """Why this `sbx` is too old, phrased for a user, or "" when it is fine."""
    if is_supported(text):
        return ""
    want = ".".join(str(n) for n in MIN_VERSION)
    return (
        f"sbx {text} is older than the supported v{want}; "
        f"amux needs `sbx ls --json`, which earlier builds do not provide"
    )


# --- naming ---


def _sanitize(part: str) -> str:
    """Reduce one identity component to characters `sbx --name` accepts."""
    cleaned = _NAME_ALLOWED.sub("-", part.strip().lower()).strip("-.+")
    return cleaned or "x"


def repo_fingerprint(repo: str | os.PathLike[str]) -> str:
    """A short, stable digest of a repository's real path.

    Two checkouts can share a workspace, task and agent name, so identity that
    stops at those three would collide across repositories. The digest is of
    the resolved path, so a symlinked checkout maps to the same sandbox.
    """
    real = os.path.realpath(os.fspath(repo))
    return hashlib.sha256(real.encode()).hexdigest()[:8]


def sandbox_name(workspace: str, task: str, agent_name: str, repo: str) -> str:
    """The deterministic sandbox name for one agent.

    Derived, not random: a crashed amux must be able to find the sandbox it
    created. The recorded sandbox *id* remains the authority for identity --
    this name only has to be reproducible and collision-free.
    """
    parts = "-".join(
        _sanitize(p) for p in (workspace, task, agent_name)
    )
    return f"amux-{parts}-{repo_fingerprint(repo)}"


def git_remote(name: str) -> str:
    """The host-side git remote `sbx create --clone` publishes for a sandbox."""
    return f"sandbox-{name}"


# --- resources ---


@dataclass(frozen=True)
class Resources:
    """Explicit per-sandbox caps.

    Deliberately not `sbx`'s defaults: `--cpus 0` means every host CPU and the
    default memory is half the host's, neither of which is a cap. Four agents
    on one laptop is the case that has to stay usable.
    """

    cpus: int = 2
    memory: str = "4g"
    share_skills: bool = False

    def validate(self) -> None:
        if not isinstance(self.cpus, int) or isinstance(self.cpus, bool):
            raise SandboxError(f"--cpus must be a whole number, got {self.cpus!r}")
        if self.cpus < 1:
            raise SandboxError(
                f"--cpus must be at least 1, got {self.cpus}; "
                "0 means 'every host CPU' to sbx, which is not a cap"
            )
        if self.cpus > 256:
            raise SandboxError(f"--cpus {self.cpus} is implausibly large")
        if not _MEMORY_RE.match(self.memory or ""):
            raise SandboxError(
                f"--memory must be a size in binary units such as 4g or 1024m, "
                f"got {self.memory!r}"
            )

    def create_flags(self) -> tuple[str, ...]:
        """The `sbx create` flags these caps imply."""
        self.validate()
        flags = ["--cpus", str(self.cpus), "--memory", self.memory]
        if not self.share_skills:
            # Docker's shared skills store is read-write and shared across
            # sandboxes, so it is opt-in rather than opt-out here.
            flags.append("--no-share-skills")
        return tuple(flags)


# --- inspection ---


def sandboxes() -> list[dict[str, Any]]:
    """Every sandbox `sbx` knows about.

    `sbx ls --json` returns `{"sandboxes": [...]}`; a missing or non-list
    `sandboxes` key means the output shape changed and must not be guessed at.
    """
    payload = _json(run("ls", "--json"), "the sandbox list")
    found = payload.get("sandboxes") if isinstance(payload, dict) else None
    if not isinstance(found, list):
        raise SandboxError(
            "unexpected `sbx ls --json` shape: expected an object with a "
            f"'sandboxes' list, got {type(payload).__name__}"
        )
    return [entry for entry in found if isinstance(entry, dict)]


def find(name: str) -> dict[str, Any] | None:
    """The recorded entry for one sandbox name, or None when absent."""
    return next((s for s in sandboxes() if s.get("name") == name), None)


def exists(name: str) -> bool:
    return find(name) is not None


def diagnose() -> dict[str, Any]:
    """`sbx diagnose -o json`, parsed.

    Runs with `check=False`: a failing check is a diagnosis, not an error, and
    the payload is what preflight needs to report.
    """
    result = run("diagnose", "-o", "json", check=False)
    payload = _json(result, "sbx diagnostics")
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        raise SandboxError("unexpected `sbx diagnose -o json` shape")
    return payload


def failed_checks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Diagnostic checks that did not pass, worst first."""
    order = {"fail": 0, "warn": 1, "skip": 2}
    bad = [
        check
        for check in report.get("checks", [])
        if isinstance(check, dict) and check.get("status") in order
    ]
    return sorted(bad, key=lambda c: order[c["status"]])


POLICY_INIT_HINT = (
    "Docker's global network policy is not initialized. "
    "This is a one-time, host-wide decision amux will not make for you; run "
    "`sbx policy init balanced` (or allow-all / deny-all) and re-run."
)


@dataclass(frozen=True)
class PolicyCheck:
    allowed: bool
    initialized: bool
    detail: str

    @property
    def remediation(self) -> str:
        if not self.initialized:
            return POLICY_INIT_HINT
        if not self.allowed:
            return ""
        return ""


def check_network(target: str, sandbox: str | None = None) -> PolicyCheck:
    """Ask whether policy would allow reaching `host:port`. Read-only.

    `sbx policy check` evaluates the same authorizer sandboxes are enforced
    against, so this answers the real question without adding a rule.
    """
    args = ["policy", "check", "network", target]
    if sandbox:
        args += ["--sandbox", sandbox]
    result = run(*args, check=False)
    detail = result.message
    if result.ok:
        return PolicyCheck(allowed=True, initialized=True, detail=detail)
    uninitialized = "not been initialized" in detail or "not initialized" in detail
    return PolicyCheck(allowed=False, initialized=not uninitialized, detail=detail)


def allow_network_command(target: str, sandbox: str | None = None) -> str:
    """The exact command a user would run to permit `host:port`.

    Returned as text to print, never executed: widening sandbox network policy
    is the user's call.
    """
    scope = f" --sandbox {sandbox}" if sandbox else ""
    return f"sbx policy allow network{scope} {target}"


# --- lifecycle ---


def create_argv(
    name: str,
    agent: str,
    repo: str,
    resources: Resources,
    clone: bool = True,
) -> tuple[str, ...]:
    """The exact `sbx create` argv for one agent's sandbox.

    Separated from execution so tests can pin the command surface without a
    Docker daemon, and so a caller can show a user what it is about to run.
    """
    if agent not in SUPPORTED_AGENTS:
        raise SandboxError(
            f"agent '{agent}' is not supported by the docker-sandbox runtime; "
            f"supported: {', '.join(SUPPORTED_AGENTS)}"
        )
    args = ["create"]
    if clone:
        args.append("--clone")
    args += ["--name", name, *resources.create_flags(), agent, repo]
    return tuple(args)


def create(
    name: str,
    agent: str,
    repo: str,
    resources: Resources,
    clone: bool = True,
) -> Sandbox:
    """Create one clone-mode sandbox and return a handle bound to its real id."""
    run(*create_argv(name, agent, repo, resources, clone=clone))
    entry = find(name)
    if entry is None:
        raise SandboxError(
            f"sbx reported success creating '{name}' but it is absent from "
            "`sbx ls --json`; refusing to guess its identity"
        )
    return Sandbox(name=name, id=str(entry.get("id") or ""), entry=entry)


def attach_argv(name: str) -> tuple[str, ...]:
    """The pane command that attaches to an existing sandbox.

    The AGENT positional is omitted deliberately: once the sandbox exists `sbx
    run --name` reattaches to the agent already inside it, which is what makes
    a stopped sandbox resumable rather than recreated.
    """
    return ("run", "--name", name)


def attach_command(name: str) -> str:
    """`attach_argv` as a shell line, for sending to a tmux pane."""
    return " ".join((SBX, *attach_argv(name)))


def stop(name: str) -> None:
    run("stop", name)


def remove(name: str, force: bool = False) -> None:
    args = ["rm"]
    if force:
        args.append("-f")
    args.append(name)
    run(*args)


# --- preflight ---


@dataclass(frozen=True)
class Check:
    """One preflight result. `remediation` is a command or action, not prose."""

    name: str
    ok: bool
    detail: str = ""
    remediation: str = ""

    def __str__(self) -> str:
        mark = "ok" if self.ok else "FAIL"
        line = f"  [{mark}] {self.name}"
        if self.detail:
            line += f": {self.detail}"
        if not self.ok and self.remediation:
            line += f"\n         fix: {self.remediation}"
        return line


@dataclass(frozen=True)
class Preflight:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.ok)

    def report(self) -> str:
        return "\n".join(str(check) for check in self.checks)

    def raise_if_failed(self) -> None:
        if self.ok:
            return
        lines = ["docker-sandbox preflight failed:"]
        lines += [str(check) for check in self.failures]
        raise SandboxError("\n".join(lines))


def is_primary_checkout(repo: str | os.PathLike[str]) -> bool:
    """True when `repo` is a repository's main checkout.

    A secondary worktree stores `.git` as a *file* pointing elsewhere, and
    `sbx create --clone` cannot clone from one. This matters in practice
    because amux runs its own agents inside secondary worktrees, so the
    obvious thing to hand the sandbox runtime is exactly the thing that
    cannot work.
    """
    return (Path(repo) / ".git").is_dir()


def preflight(
    *,
    agents: Sequence[str],
    repo: str,
    resources: Resources,
    endpoint: str,
    service_healthy: Callable[[], tuple[bool, str]] | None = None,
) -> Preflight:
    """Check everything that must hold before a sandbox grid is created.

    Read-only by construction: every `sbx` subcommand used here (`version`,
    `ls`, `diagnose`, `policy check`) inspects state, and none of them creates
    a sandbox, writes policy, or signs anything in. Nothing here touches tmux,
    git, or the database either -- the point is that a failure leaves the host
    exactly as it was.

    `service_healthy` is injected rather than imported so preflight does not
    depend on the context service being importable, and so the whole check can
    run in tests without a listener.
    """
    checks: list[Check] = []

    # 1. Resource values and agent kinds are pure local validation, so they run
    #    first: no reason to probe Docker to reject `--cpus 0`.
    try:
        resources.validate()
        checks.append(
            Check("resources", True, f"{resources.cpus} cpu, {resources.memory}")
        )
    except SandboxError as exc:
        checks.append(Check("resources", False, str(exc), "correct the resource flags"))

    unsupported = sorted({a for a in agents if a not in SUPPORTED_AGENTS})
    checks.append(
        Check(
            "agents",
            not unsupported,
            ", ".join(agents) if not unsupported else f"unsupported: {', '.join(unsupported)}",
            f"the docker-sandbox runtime supports {' and '.join(SUPPORTED_AGENTS)}; "
            "spawn the others with the default host runtime",
        )
    )

    # 2. The repository, before anything external.
    if not repo:
        checks.append(
            Check("repository", False, "no git repository", "spawn inside a git repository")
        )
    elif not is_primary_checkout(repo):
        checks.append(
            Check(
                "repository",
                False,
                f"{repo} is not a primary checkout",
                "sbx create --clone cannot clone a secondary git worktree; "
                "point --path at the repository's main checkout",
            )
        )
    else:
        checks.append(Check("repository", True, repo))

    # 3. sbx itself: presence, then whether this build has the surface amux
    #    parses. A missing executable makes every later check meaningless, so
    #    they are skipped rather than reported as spurious failures.
    try:
        detected = version()
    except SandboxError as exc:
        checks.append(
            Check(
                "sbx",
                False,
                str(exc),
                "install Docker Sandboxes, or spawn without --runtime docker-sandbox",
            )
        )
        return Preflight(tuple(checks))

    reason = unsupported_reason(detected)
    checks.append(
        Check(
            "sbx",
            not reason,
            reason or detected,
            f"upgrade Docker Sandboxes to v{'.'.join(str(n) for n in MIN_VERSION)} "
            "or newer" if reason else "",
        )
    )

    # 4. Docker's own diagnosis, including authentication.
    try:
        report = diagnose()
        bad = failed_checks(report)
        blocking = [c for c in bad if c.get("status") == "fail"]
        checks.append(
            Check(
                "docker",
                not blocking,
                "; ".join(f"{c['name']}: {c.get('message', '')}" for c in bad)
                or "all checks pass",
                "; ".join(c["hint"] for c in blocking if c.get("hint"))
                or "run `sbx diagnose` for detail",
            )
        )
    except SandboxError as exc:
        checks.append(Check("docker", False, str(exc), "run `sbx diagnose`"))

    # 5. The context service, and whether a sandbox could actually reach it.
    #    Both matter: a healthy service behind a policy that blocks the port is
    #    just as broken as no service at all.
    if service_healthy is not None:
        healthy, detail = service_healthy()
        checks.append(
            Check(
                "context-service",
                healthy,
                detail,
                "start it with `amux context-service start`",
            )
        )

    policy = check_network(endpoint)
    checks.append(
        Check(
            "network-policy",
            policy.allowed,
            policy.detail,
            policy.remediation or allow_network_command(endpoint),
        )
    )
    return Preflight(tuple(checks))


@dataclass(frozen=True)
class Sandbox:
    """A live sandbox handle.

    Satisfies the `SandboxOps` protocol that `sandbox_bootstrap` drives, so
    bootstrap never shells out to `sbx` itself and can be tested against a
    stub.
    """

    name: str
    id: str = ""
    entry: dict[str, Any] | None = None

    @property
    def git_remote(self) -> str:
        return git_remote(self.name)

    def copy_in(self, source: Path, destination: str) -> None:
        """Copy a host file to an absolute path inside the sandbox.

        This is the delivery path for the context token: the secret travels as
        a file, so it never appears in argv.
        """
        if not destination.startswith("/"):
            raise SandboxError(
                f"sandbox destination must be absolute, got {destination!r}"
            )
        run("cp", os.fspath(source), f"{self.name}:{destination}")

    def exec(self, argv: Sequence[str]) -> str:
        """Run a command inside the sandbox and return its stdout."""
        if not argv:
            raise SandboxError("exec needs a command")
        return run("exec", self.name, *argv).stdout

    def refresh(self) -> Sandbox:
        entry = find(self.name)
        if entry is None:
            raise SandboxError(f"sandbox '{self.name}' is gone")
        return Sandbox(name=self.name, id=str(entry.get("id") or ""), entry=entry)
