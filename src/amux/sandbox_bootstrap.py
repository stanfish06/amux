from __future__ import annotations

import json
import os
import posixpath
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from amux import sandbox_hooks, shared
from amux.sandbox_client import CONFIG_ENV  # noqa: F401  (re-export: one env name)

SHIM_PATH = "/usr/local/bin/amux"
CONFIG_RELPATH = ".config/amux/context.json"

CONFIG_MODE = "600"
SHIM_MODE = "755"
HOOK_MODE = "644"
SKILL_MODE = "644"


class BootstrapError(Exception):
    pass


class HookMergeErrorFromImage(BootstrapError):
    pass


class SandboxOps(Protocol):
    name: str

    def copy_in(self, source: Path, destination: str) -> None: ...

    def exec(self, argv: Sequence[str], *, user: str | None = None) -> str: ...


@dataclass(frozen=True)
class SandboxIdentity:
    home: str
    user: str
    group: str

    @property
    def owner(self) -> str:
        return f"{self.user}:{self.group}"


@dataclass(frozen=True)
class Installed:
    shim_path: str
    config_path: str
    home: str


@dataclass(frozen=True)
class SkillInstalled:
    path: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.path)


@dataclass(frozen=True)
class HostSkillInstalled:
    path: str = ""
    reason: str = ""
    changed: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.path)


@dataclass(frozen=True)
class HooksInstalled:
    agent: str
    settings_path: str
    missing_kinds: tuple[str, ...]
    location_verified: bool
    previous_notify: tuple[str, ...] | None = None
    agent_version: str = ""
    mechanism: str = "hooks"

    @property
    def degraded(self) -> bool:
        return bool(self.missing_kinds)


def default_staging_dir() -> Path:
    return shared.STATE_DIR / "staging"


CLIENT_MODULE = "sandbox_client.py"


def client_source() -> Path:
    from amux import sandbox_client

    source = Path(sandbox_client.__file__ or "")
    if not source.is_file():
        raise BootstrapError(
            f"the sandbox client source is missing at {source}, so there is nothing "
            f"to install into a sandbox. A packaged amux must ship it: "
            f"pyinstaller --add-data <repo>/src/amux/{CLIENT_MODULE}:amux"
        )
    return source


SKILL_NAME = "amux"
SKILL_FILE = "SKILL.md"


def skill_source() -> Path:
    from amux import sandbox_client

    package = Path(sandbox_client.__file__ or "").parent
    relative = Path("skills") / SKILL_NAME / SKILL_FILE
    candidates = (package / relative, package.parents[1] / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BootstrapError(
        f"amux's own skill is missing: looked for {SKILL_FILE} at "
        + ", ".join(str(c) for c in candidates)
        + f". A packaged amux must ship it: pyinstaller --add-data "
        f"<repo>/{relative}:{posixpath.join('amux', 'skills', SKILL_NAME)}"
    )


def install_skill(
    ops: SandboxOps,
    agent: str,
    installed: Installed,
    *,
    source: Path | None = None,
) -> SkillInstalled:
    try:
        document = source or skill_source()
        destination = sandbox_skill_destination(agent, installed)
        who = _identity(ops, "")
        _exec(ops, "", ["mkdir", "-p", posixpath.dirname(destination)])
        _deliver(ops, "", document, destination, who, SKILL_MODE)
        return SkillInstalled(path=destination)
    except (BootstrapError, sandbox_hooks.HookMergeError) as exc:
        return SkillInstalled(reason=str(exc))


def sandbox_skill_destination(agent: str, installed: Installed) -> str:
    skills = sandbox_hooks.hooks_for(agent).skills_relpath
    return posixpath.join(installed.home, skills, SKILL_NAME, SKILL_FILE)


def host_skill_destination(agent: str, *, home: Path | None = None) -> Path:
    skills = sandbox_hooks.hooks_for(agent).skills_relpath
    root = Path.home() if home is None else home
    return root / skills / SKILL_NAME / SKILL_FILE


def install_host_skill(
    agent: str,
    *,
    home: Path | None = None,
    source: Path | None = None,
) -> HostSkillInstalled:
    try:
        document = source or skill_source()
        destination = host_skill_destination(agent, home=home)
        payload = document.read_bytes()
        changed = _clear_host_destination(destination, payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        destination.chmod(int(SKILL_MODE, 8))
        return HostSkillInstalled(path=str(destination), changed=changed)
    except Exception as exc:  # noqa: BLE001
        return HostSkillInstalled(reason=str(exc) or type(exc).__name__)


def _clear_host_destination(destination: Path, payload: bytes) -> bool:
    replaced = False
    for path in (destination.parent, destination):
        if path.is_symlink():
            path.unlink()
            replaced = True
    if replaced:
        return True
    try:
        return destination.read_bytes() != payload
    except OSError:
        return True


def stage_config_file(endpoint: str, token: str, *, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "context.json"
    document = json.dumps({"endpoint": endpoint, "token": token})
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise BootstrapError(
            f"refusing to reuse the staging path {path}: it already holds "
            f"capability material from an earlier bootstrap"
        ) from exc
    except OSError as exc:
        raise BootstrapError(
            f"cannot stage the sandbox config at {path}: {exc}"
        ) from exc
    with os.fdopen(fd, "w") as handle:
        handle.write(document)
    return path


def install_client(
    ops: SandboxOps,
    *,
    endpoint: str,
    token: str,
    staging_dir: Path | None = None,
    source: Path | None = None,
) -> Installed:
    staging = staging_dir or default_staging_dir()
    shim = source or client_source()
    staged = stage_config_file(endpoint, token, directory=staging)
    try:
        who = _identity(ops, token)
        config_path = posixpath.join(who.home, CONFIG_RELPATH)
        _exec(ops, token, ["mkdir", "-p", posixpath.dirname(config_path)])
        _deliver(ops, token, staged, config_path, who, CONFIG_MODE)
        _deliver(ops, token, shim, SHIM_PATH, who, SHIM_MODE)
        _exec(ops, token, [SHIM_PATH, "--help"])
        return Installed(shim_path=SHIM_PATH, config_path=config_path, home=who.home)
    finally:
        staged.unlink(missing_ok=True)


def install_hooks(
    ops: SandboxOps,
    agent: str,
    installed: Installed,
    *,
    staging_dir: Path | None = None,
) -> HooksInstalled:
    adapter = sandbox_hooks.hooks_for(agent)
    staging = staging_dir or default_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)

    version = ""
    hooks_supported = True
    if agent == sandbox_hooks.CODEX.agent:
        version = _agent_version(ops, "codex")
        hooks_supported = sandbox_hooks.codex_supports_hooks(version)

    if hooks_supported:
        return _install_hook_document(ops, adapter, installed, staging, version)
    return _install_codex_notify_fallback(ops, adapter, installed, staging, version)


def _install_hook_document(
    ops: SandboxOps,
    adapter: sandbox_hooks.AgentHooks,
    installed: Installed,
    staging: Path,
    version: str,
) -> HooksInstalled:
    who = _identity(ops, "")
    settings_path = posixpath.join(installed.home, adapter.settings_relpath)
    existing = _read_optional(ops, settings_path)
    document = _parse_json(existing, settings_path)
    text = sandbox_hooks.render_hook_settings(
        sandbox_hooks.merge_hook_settings(
            document,
            adapter,
            shim=installed.shim_path,
            config_path=installed.config_path,
        )
    )
    _exec(ops, "", ["mkdir", "-p", posixpath.dirname(settings_path)])
    _write_into(ops, staging, settings_path, text, who, HOOK_MODE)

    if adapter.enable_relpath:
        enable_path = posixpath.join(installed.home, adapter.enable_relpath)
        current = _read_optional(ops, enable_path) or ""
        switched = sandbox_hooks.enable_codex_hooks(current)
        if switched != current:
            _exec(ops, "", ["mkdir", "-p", posixpath.dirname(enable_path)])
            _write_into(ops, staging, enable_path, switched, who, HOOK_MODE)

    return HooksInstalled(
        agent=adapter.agent,
        settings_path=settings_path,
        missing_kinds=sandbox_hooks.missing_kinds(adapter.agent, hooks_supported=True),
        location_verified=not adapter.paths_are_assumed,
        agent_version=version,
        mechanism="hooks",
    )


def _install_codex_notify_fallback(
    ops: SandboxOps,
    adapter: sandbox_hooks.AgentHooks,
    installed: Installed,
    staging: Path,
    version: str,
) -> HooksInstalled:
    assert adapter.enable_relpath is not None
    who = _identity(ops, "")
    config_path = posixpath.join(installed.home, adapter.enable_relpath)
    existing = _read_optional(ops, config_path) or ""
    text, replaced = sandbox_hooks.merge_codex_config(existing)
    _write_into(
        ops,
        staging,
        sandbox_hooks.CODEX_DISPATCH_PATH,
        sandbox_hooks.render_codex_dispatch(
            installed.shim_path, installed.config_path, list(replaced or ())
        ),
        who,
        SHIM_MODE,
    )
    _exec(ops, "", ["mkdir", "-p", posixpath.dirname(config_path)])
    _write_into(ops, staging, config_path, text, who, HOOK_MODE)
    return HooksInstalled(
        agent=adapter.agent,
        settings_path=config_path,
        missing_kinds=sandbox_hooks.missing_kinds(adapter.agent, hooks_supported=False),
        location_verified=not adapter.paths_are_assumed,
        previous_notify=tuple(replaced) if replaced else None,
        agent_version=version,
        mechanism="notify",
    )


def _parse_json(text: str | None, path: str) -> dict | None:
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HookMergeErrorFromImage(
            f"{path} in the sandbox image is not valid JSON: {exc}"
        ) from None


def _agent_version(ops: SandboxOps, binary: str) -> str:
    try:
        return ops.exec(
            ["sh", "-lc", f"{shlex.quote(binary)} --version 2>/dev/null"]
        ).strip()
    except Exception:
        return ""


def _read_optional(ops: SandboxOps, path: str) -> str | None:
    try:
        return ops.exec(["sh", "-lc", f"cat {shlex.quote(path)} 2>/dev/null || true"])
    except Exception as exc:
        raise _fail(ops, "", f"reading {path}", exc) from None


def _write_into(
    ops: SandboxOps,
    staging: Path,
    destination: str,
    text: str,
    who: SandboxIdentity,
    mode: str,
) -> None:
    local = staging / posixpath.basename(destination)
    local.write_text(text)
    try:
        _deliver(ops, "", local, destination, who, mode)
    finally:
        local.unlink(missing_ok=True)


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _fail(ops: SandboxOps, token: str, what: str, exc: BaseException) -> BootstrapError:
    return BootstrapError(
        f"sandbox {ops.name}: {what} failed: {_redact(str(exc), token)}"
    )


def _exec(
    ops: SandboxOps, token: str, argv: Sequence[str], *, user: str | None = None
) -> str:
    try:
        return ops.exec(argv, user=user) if user else ops.exec(argv)
    except Exception as exc:
        where = " ".join(argv) if user is None else f"{' '.join(argv)} (as {user})"
        raise _fail(ops, token, where, exc) from None


def _copy(ops: SandboxOps, token: str, source: Path, destination: str) -> None:
    try:
        ops.copy_in(source, destination)
    except Exception as exc:
        raise _fail(ops, token, f"copy to {destination}", exc) from None


def _identity(ops: SandboxOps, token: str) -> SandboxIdentity:
    raw = _exec(
        ops,
        token,
        ["sh", "-lc", 'printf "%s\\n%s\\n%s" "$HOME" "$(id -un)" "$(id -gn)"'],
    )
    parts = raw.strip().splitlines()
    if len(parts) != 3 or not parts[0].startswith("/") or not all(parts):
        raise BootstrapError(
            f"sandbox {ops.name}: could not resolve the agent's identity "
            f"(got {raw!r}); the context client config has nowhere to go"
        )
    return SandboxIdentity(home=parts[0], user=parts[1], group=parts[2])


def _deliver(
    ops: SandboxOps,
    token: str,
    source: Path,
    destination: str,
    who: SandboxIdentity,
    mode: str,
) -> None:
    _copy(ops, token, source, destination)
    _exec(ops, token, ["chown", who.owner, destination], user="root")
    _exec(ops, token, ["chmod", mode, destination])
