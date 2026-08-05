"""Host-side bootstrap: put the amux shim and its capability inside a sandbox.

This runs on the host, so unlike `sandbox_client` it may import from amux. It
does *not* shell out to `sbx`: every sandbox operation goes through an injected
`SandboxOps`, which `sandbox.py` implements over `sbx cp` and `sbx exec`. That
keeps one owner for the `sbx` command surface and lets the security properties
below be tested without Docker.

Two properties this module exists to guarantee:

- The plaintext capability token is written once, at mode `0600`, through
  `O_CREAT | O_EXCL` — never chmod-ed after the fact, which would leave a window
  where another local user could read it — and is removed from the host staging
  path on every exit path, success or failure.
- The token never appears in an argument, a path, an environment variable, or a
  diagnostic. `sbx cp` moves a *file*; the secret is only ever the file's
  contents. This module is also the last layer that still knows the token, so it
  is where transport diagnostics get scrubbed.
"""

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

#: Where the shim lands inside the VM. On PATH, so `amux` just works, and the
#: agent's own hooks can name it absolutely.
SHIM_PATH = "/usr/local/bin/amux"
#: Config location relative to the agent's home, matching the shim's default
#: `$XDG_CONFIG_HOME/amux/context.json` lookup.
CONFIG_RELPATH = ".config/amux/context.json"

CONFIG_MODE = "600"
SHIM_MODE = "755"
#: Hook documents are configuration, not secrets: the agent must own and be able
#: to rewrite them, and its own tooling reads them.
HOOK_MODE = "644"


class BootstrapError(Exception):
    """A sandbox operation failed, or refused to proceed."""


class HookMergeErrorFromImage(BootstrapError):
    """The image shipped agent configuration amux will not silently overwrite."""


class SandboxOps(Protocol):
    """The sandbox operations bootstrap needs. `sandbox.py` implements this.

    Both methods raise on failure — `BootstrapError`, or any exception, which
    this module wraps and redacts. `exec` returns stdout.
    """

    name: str

    def copy_in(self, source: Path, destination: str) -> None:
        """Copy a host file to an absolute path inside the sandbox."""
        ...

    def exec(self, argv: Sequence[str], *, user: str | None = None) -> str:
        """Run a command inside the sandbox and return its stdout.

        `user` is `None` for the sandbox's own agent user — the default and the
        only thing bootstrap wants for almost everything. `"root"` is used solely
        to chown copied-in files (see `_deliver`); nothing else is escalated.
        """
        ...


@dataclass(frozen=True)
class SandboxIdentity:
    """Who the sandbox's agent is, asked rather than assumed.

    The Claude and Codex images do not have to agree on any of it: Codex's
    runs as `agent` with `HOME=/home/agent`, not `root`.
    """

    home: str
    user: str
    group: str

    @property
    def owner(self) -> str:
        return f"{self.user}:{self.group}"


@dataclass(frozen=True)
class Installed:
    """Where the shim and its capability ended up inside the sandbox."""

    shim_path: str
    config_path: str
    home: str


@dataclass(frozen=True)
class HooksInstalled:
    """The result of wiring one agent's hooks, including what it cannot report.

    `missing_kinds` is not a warning to be swallowed. It is empty for Claude and
    for any Codex new enough to have `hooks.json`; it is non-empty only for a
    Codex old enough to be limited to the single `notify` slot, which is
    *detected* in the image rather than assumed. A caller that presents a
    degraded sandbox's resolved state as authoritative is claiming accuracy it
    does not have.
    """

    agent: str
    settings_path: str
    missing_kinds: tuple[str, ...]
    location_verified: bool
    previous_notify: tuple[str, ...] | None = None
    #: Version string the image reported, for diagnostics. Empty if unavailable.
    agent_version: str = ""
    #: Which mechanism was installed: "hooks" or the older "notify" fallback.
    mechanism: str = "hooks"

    @property
    def degraded(self) -> bool:
        return bool(self.missing_kinds)


def default_staging_dir() -> Path:
    """Under the amux state directory, not `/tmp`: the token lives here for
    milliseconds and the state directory is already the private one.

    Read through `shared` on every call rather than bound at import: a
    `from ... import STATE_DIR` creates a second name that test isolation
    patching `shared.STATE_DIR` cannot reach, which is how a test ends up
    writing capability files into the live state directory.
    """
    return shared.STATE_DIR / "staging"


def client_source() -> Path:
    """The shim file to copy in."""
    from amux import sandbox_client

    source = Path(sandbox_client.__file__ or "")
    if not source.is_file():
        raise BootstrapError(f"sandbox client source is missing at {source}")
    return source


def stage_config_file(endpoint: str, token: str, *, directory: Path) -> Path:
    """Write the shim's `{endpoint, token}` config to a private host file.

    Created exclusively at mode 0600 in one step. An existing file is a refusal,
    not an overwrite: it means a previous bootstrap left plaintext behind and
    something is wrong with the caller's cleanup.
    """
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
        raise BootstrapError(f"cannot stage the sandbox config at {path}: {exc}") from exc
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
    """Install the shim and deliver its capability into one sandbox.

    Ordering matters: the config directory exists before anything is copied into
    it, every copy is chowned before its mode is set (see `_deliver`), and the
    host's plaintext copy is removed in `finally`, so a failure anywhere above
    still leaves nothing behind.

    That last property is not theoretical. Before the chown existed, this method
    failed against a real image at `chmod 755` on the shim — and it failed
    *closed*: the host staging file was removed by the `finally` below and no
    world-readable token was ever created inside the VM.
    """
    staging = staging_dir or default_staging_dir()
    shim = source or client_source()
    staged = stage_config_file(endpoint, token, directory=staging)
    try:
        who = _identity(ops, token)
        config_path = posixpath.join(who.home, CONFIG_RELPATH)
        _exec(ops, token, ["mkdir", "-p", posixpath.dirname(config_path)])
        _deliver(ops, token, staged, config_path, who, CONFIG_MODE)
        _deliver(ops, token, shim, SHIM_PATH, who, SHIM_MODE)
        # The shim must at least run under the VM's interpreter. This needs no
        # service and no capability, so it isolates "copied wrong" from "cannot
        # reach the host" before an agent ever sees a context command fail.
        _exec(ops, token, [SHIM_PATH, "--help"])
        return Installed(shim_path=SHIM_PATH, config_path=config_path, home=who.home)
    finally:
        staged.unlink(missing_ok=True)


def install_hooks(
    ops: SandboxOps, agent: str, installed: Installed, *, staging_dir: Path | None = None
) -> HooksInstalled:
    """Merge amux's hooks into one agent's sandbox-local configuration.

    Reads whatever the image ships, merges only amux's own entries into it, and
    writes it back. Nothing of the user's host configuration is copied: it names
    host paths and host tools that do not exist in a microVM.

    No capability material is involved, so unlike `install_client` there is
    nothing here to scrub — the hook command carries the config file's *path*.
    """
    adapter = sandbox_hooks.hooks_for(agent)
    staging = staging_dir or default_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)

    version = ""
    hooks_supported = True
    if agent == sandbox_hooks.CODEX.agent:
        # Detected, not assumed: an old Codex has only the single `notify` slot,
        # a current one has a full hook surface, and the image's version is the
        # one fact that decides which.
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
    """The normal path for both agents: merge into the agent's hook document."""
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
        # Codex: hooks.json is inert until `hooks = true` under [features].
        enable_path = posixpath.join(installed.home, adapter.enable_relpath)
        current = _read_optional(ops, enable_path) or ""
        switched = sandbox_hooks.enable_codex_hooks(current)
        if switched != current:
            _exec(ops, "", ["mkdir", "-p", posixpath.dirname(enable_path)])
            # Chowned like everything else, and here it matters beyond permissions:
            # a config.toml the agent does not own is one `codex features enable`
            # cannot write, and one Codex cannot persist its own hook trust into.
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
    """A Codex too old for `hooks.json`: use its single `notify` slot and report
    exactly which kinds that cannot cover."""
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
    """`<binary> --version` inside the VM, or "" when it cannot be asked.

    An empty answer is treated as "no hook support" downstream, which falls back
    to a mechanism that works and says what it misses — better than assuming a
    hook surface that may not be there and reporting nothing at all.
    """
    try:
        return ops.exec(["sh", "-lc", f"{shlex.quote(binary)} --version 2>/dev/null"]).strip()
    except Exception:
        return ""


def _read_optional(ops: SandboxOps, path: str) -> str | None:
    """The file's contents, or None when the image does not ship one. A missing
    file is the expected case and must not look like a failure."""
    try:
        return ops.exec(["sh", "-lc", f'cat {shlex.quote(path)} 2>/dev/null || true'])
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
    """Deliver text as a file rather than through a shell heredoc: an argument
    would put the whole document in the process table and make quoting a
    correctness problem."""
    local = staging / posixpath.basename(destination)
    local.write_text(text)
    try:
        _deliver(ops, "", local, destination, who, mode)
    finally:
        local.unlink(missing_ok=True)


# --- sandbox operations, with the token scrubbed from every diagnostic -------


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
    """Who the sandbox's agent is: `$HOME`, user, group. One probe, because all
    three are the same question and each `sbx exec` is a round trip.

    Asked rather than assumed. Codex's image runs as `agent` with
    `HOME=/home/agent`; a hardcoded `root` would put the capability where the
    shim never looks and chown it to a user that is not the one running.
    """
    raw = _exec(
        ops, token, ["sh", "-lc", 'printf "%s\\n%s\\n%s" "$HOME" "$(id -un)" "$(id -gn)"']
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
    """Copy a file in, then make it the agent's own at `mode`.

    The chown is not optional and not cosmetic. `sbx cp` preserves the source
    mode but sets the owner to the *host* uid, which does not exist inside the
    container — verified against a live image, where a 0600 capability landed as
    `-rw------- 501:root` and the agent got "Permission denied" reading its own
    token. It also cannot chmod a file it does not own, so the chown must come
    first or the chmod fails with EPERM.

    Only this chown runs as root; everything else bootstrap does runs as the
    agent.

    A trap for whoever changes `CONFIG_MODE`: before the chown existed, the
    `chmod 600` on the capability *appeared* to succeed while the shim's
    `chmod 755` failed. That was an accident of coreutils, which elides the
    chmod(2) syscall when the mode already matches — and the staged file is
    already 0600. `chmod 601` on the same file failed EPERM, proving the point.
    So the real fault surfaced two steps later, at the shim, pointing a reader
    at `/usr/local/bin` instead of at ownership. Do not read a passing chmod as
    evidence that ownership is right.
    """
    _copy(ops, token, source, destination)
    _exec(ops, token, ["chown", who.owner, destination], user="root")
    _exec(ops, token, ["chmod", mode, destination])
