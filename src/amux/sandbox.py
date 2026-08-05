"""Docker Sandbox (`sbx`) adapter."""

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

MIN_VERSION = (0, 37, 0)

SUPPORTED_AGENTS = ("claude", "codex")

DEFAULT_TIMEOUT_S = 120.0

_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9.+-]+")
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
        raise SandboxError(
            f"sbx {' '.join(args)} timed out after {timeout:g}s"
        ) from exc

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


def version() -> str:
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
    if is_supported(text):
        return ""
    want = ".".join(str(n) for n in MIN_VERSION)
    return (
        f"sbx {text} is older than the supported v{want}; "
        f"amux needs `sbx ls --json`, which earlier builds do not provide"
    )


def _sanitize(part: str) -> str:
    cleaned = _NAME_ALLOWED.sub("-", part.strip().lower()).strip("-.+")
    return cleaned or "x"


def repo_fingerprint(repo: str | os.PathLike[str]) -> str:
    real = os.path.realpath(os.fspath(repo))
    return hashlib.sha256(real.encode()).hexdigest()[:8]


def sandbox_name(workspace: str, task: str, agent_name: str, repo: str) -> str:
    parts = "-".join(_sanitize(p) for p in (workspace, task, agent_name))
    return f"amux-{parts}-{repo_fingerprint(repo)}"


def git_remote(name: str) -> str:
    return f"sandbox-{name}"


@dataclass(frozen=True)
class Resources:
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
            flags.append("--no-share-skills")
        return tuple(flags)


def sandboxes() -> list[dict[str, Any]]:
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


GIT_DAEMON_PORT = 9418


def published_ports(entry: dict[str, Any]) -> list[dict[str, Any]]:
    ports = entry.get("ports") or []
    return [p for p in ports if isinstance(p, dict)]


def git_url(name: str, repo: str) -> str | None:
    entry = find(name)
    if entry is None:
        return None
    for port in published_ports(entry):
        if port.get("sandbox_port") != GIT_DAEMON_PORT:
            continue
        host = port.get("host_ip") or "127.0.0.1"
        published = port.get("host_port")
        if not published:
            continue
        return f"git://{host}:{published}/{os.path.basename(repo.rstrip('/'))}"
    return None


def diagnose() -> dict[str, Any]:
    result = run("diagnose", "-o", "json", check=False)
    payload = _json(result, "sbx diagnostics")
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        raise SandboxError("unexpected `sbx diagnose -o json` shape")
    return payload


def failed_checks(report: dict[str, Any]) -> list[dict[str, Any]]:
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
    scope = f" --sandbox {sandbox}" if sandbox else ""
    return f"sbx policy allow network{scope} {target}"


def create_argv(
    name: str,
    agent: str,
    repo: str,
    resources: Resources,
    clone: bool = True,
) -> tuple[str, ...]:
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
    run(*create_argv(name, agent, repo, resources, clone=clone))
    entry = find(name)
    if entry is None:
        raise SandboxError(
            f"sbx reported success creating '{name}' but it is absent from "
            "`sbx ls --json`; refusing to guess its identity"
        )
    return Sandbox(name=name, id=str(entry.get("id") or ""), entry=entry)


# Codex SKIPS an untrusted hook silently — no prompt, no warning — so without
# this a sandboxed Codex never reports state and reads permanently idle. Safe
# only because amux authors the sole hooks.json and the VM is the boundary;
# revisit if user-supplied hooks are ever allowed.
HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"

AGENT_ATTACH_ARGS: dict[str, tuple[str, ...]] = {"codex": (HOOK_TRUST_FLAG,)}


def attach_argv(name: str, agent: str = "") -> tuple[str, ...]:
    args = ["run", "--name", name]
    extra = AGENT_ATTACH_ARGS.get(agent, ())
    if extra:
        args += [agent, "--", *extra]
    return tuple(args)


def attach_command(name: str, agent: str = "") -> str:
    return " ".join((SBX, *attach_argv(name, agent)))


def stop(name: str) -> None:
    run("stop", name)


def remove(name: str, force: bool = False) -> None:
    args = ["rm"]
    if force:
        args.append("-f")
    args.append(name)
    run(*args)


@dataclass(frozen=True)
class Check:
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
    return (Path(repo) / ".git").is_dir()


def _client_source_check() -> Check:
    from amux import sandbox_bootstrap

    try:
        source = sandbox_bootstrap.client_source()
    except Exception as exc:  # noqa: BLE001 - any resolver failure is the same fault
        return Check(
            "context-client",
            False,
            str(exc),
            "this amux build does not ship the sandbox context client; "
            "rebuild with it bundled (PyInstaller needs it added as data, since "
            "a frozen bundle contains no .py source to fall back on) or run "
            "amux from a source checkout",
        )
    return Check("context-client", True, str(source))


def preflight(
    *,
    agents: Sequence[str],
    repo: str,
    resources: Resources,
    endpoint: str,
    service_healthy: Callable[[], tuple[bool, str]] | None = None,
) -> Preflight:
    checks: list[Check] = []
    try:
        resources.validate()
        checks.append(
            Check("resources", True, f"{resources.cpus} cpu, {resources.memory}")
        )
    except SandboxError as exc:
        checks.append(Check("resources", False, str(exc), "correct the resource flags"))

    checks.append(_client_source_check())

    unsupported = sorted({a for a in agents if a not in SUPPORTED_AGENTS})
    checks.append(
        Check(
            "agents",
            not unsupported,
            ", ".join(agents)
            if not unsupported
            else f"unsupported: {', '.join(unsupported)}",
            f"the docker-sandbox runtime supports {' and '.join(SUPPORTED_AGENTS)}; "
            "spawn the others with the default host runtime",
        )
    )

    if not repo:
        checks.append(
            Check(
                "repository",
                False,
                "no git repository",
                "spawn inside a git repository",
            )
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
            "or newer"
            if reason
            else "",
        )
    )

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
    name: str
    id: str = ""
    entry: dict[str, Any] | None = None

    @property
    def git_remote(self) -> str:
        return git_remote(self.name)

    def copy_in(self, source: Path, destination: str) -> None:
        if not destination.startswith("/"):
            raise SandboxError(
                f"sandbox destination must be absolute, got {destination!r}"
            )
        run("cp", os.fspath(source), f"{self.name}:{destination}")

    def exec(self, argv: Sequence[str], *, user: str | None = None) -> str:
        if not argv:
            raise SandboxError("exec needs a command")
        flags = ("-u", user) if user else ()
        return run("exec", *flags, self.name, *argv).stdout

    def wake(self) -> None:
        self.exec(["true"])

    def working_tree_status(self) -> str:
        return self.exec(["git", "status", "--porcelain"]).strip()

    def refresh(self) -> Sandbox:
        entry = find(self.name)
        if entry is None:
            raise SandboxError(f"sandbox '{self.name}' is gone")
        return Sandbox(name=self.name, id=str(entry.get("id") or ""), entry=entry)
