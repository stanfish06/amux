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


class BootstrapError(Exception):
    """A sandbox operation failed, or refused to proceed."""


class SandboxOps(Protocol):
    """The sandbox operations bootstrap needs. `sandbox.py` implements this.

    Both methods raise on failure — `BootstrapError`, or any exception, which
    this module wraps and redacts. `exec` returns stdout.
    """

    name: str

    def copy_in(self, source: Path, destination: str) -> None:
        """Copy a host file to an absolute path inside the sandbox."""
        ...

    def exec(self, argv: Sequence[str]) -> str:
        """Run a command inside the sandbox and return its stdout."""
        ...


@dataclass(frozen=True)
class Installed:
    """Where the shim and its capability ended up inside the sandbox."""

    shim_path: str
    config_path: str
    home: str


@dataclass(frozen=True)
class HooksInstalled:
    """The result of wiring one agent's hooks, including what it cannot report.

    `missing_kinds` is not a warning to be swallowed: for Codex it is always
    non-empty, because that agent has one end-of-turn notification slot and no
    per-tool event. A caller that presents such a sandbox's resolved state as
    being as live as a Claude one is claiming accuracy it does not have.
    """

    agent: str
    settings_path: str
    missing_kinds: tuple[str, ...]
    location_verified: bool
    previous_notify: tuple[str, ...] | None = None

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
    it, and the config file's mode is tightened before the shim is usable. The
    host's plaintext copy is removed in `finally`, so a failure anywhere above
    still leaves nothing behind.
    """
    staging = staging_dir or default_staging_dir()
    shim = source or client_source()
    staged = stage_config_file(endpoint, token, directory=staging)
    try:
        home = _home(ops, token)
        config_path = posixpath.join(home, CONFIG_RELPATH)
        _exec(ops, token, ["mkdir", "-p", posixpath.dirname(config_path)])
        _copy(ops, token, staged, config_path)
        _exec(ops, token, ["chmod", CONFIG_MODE, config_path])
        _copy(ops, token, shim, SHIM_PATH)
        _exec(ops, token, ["chmod", SHIM_MODE, SHIM_PATH])
        # The shim must at least run under the VM's interpreter. This needs no
        # service and no capability, so it isolates "copied wrong" from "cannot
        # reach the host" before an agent ever sees a context command fail.
        _exec(ops, token, [SHIM_PATH, "--help"])
        return Installed(shim_path=SHIM_PATH, config_path=config_path, home=home)
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
    settings_path = posixpath.join(installed.home, adapter.settings_relpath)
    existing = _read_optional(ops, settings_path)
    previous: tuple[str, ...] | None = None

    if adapter.format == "json":
        document = json.loads(existing) if existing and existing.strip() else None
        text = sandbox_hooks.render_claude_settings(
            sandbox_hooks.merge_claude_settings(
                document, shim=installed.shim_path, config_path=installed.config_path
            )
        )
    else:
        text, replaced = sandbox_hooks.merge_codex_config(existing or "")
        previous = tuple(replaced) if replaced else None
        _write_into(
            ops,
            staging,
            "amux-codex-notify",
            sandbox_hooks.CODEX_DISPATCH_PATH,
            sandbox_hooks.render_codex_dispatch(
                installed.shim_path, installed.config_path, list(replaced or ())
            ),
        )
        _exec(ops, "", ["chmod", "755", sandbox_hooks.CODEX_DISPATCH_PATH])

    _exec(ops, "", ["mkdir", "-p", posixpath.dirname(settings_path)])
    _write_into(ops, staging, posixpath.basename(settings_path), settings_path, text)
    return HooksInstalled(
        agent=agent,
        settings_path=settings_path,
        missing_kinds=sandbox_hooks.missing_kinds(agent),
        location_verified=not adapter.paths_are_assumed,
        previous_notify=previous,
    )


def _read_optional(ops: SandboxOps, path: str) -> str | None:
    """The file's contents, or None when the image does not ship one. A missing
    file is the expected case and must not look like a failure."""
    try:
        return ops.exec(["sh", "-lc", f'cat {shlex.quote(path)} 2>/dev/null || true'])
    except Exception as exc:
        raise _fail(ops, "", f"reading {path}", exc) from None


def _write_into(
    ops: SandboxOps, staging: Path, filename: str, destination: str, text: str
) -> None:
    """Deliver text as a file rather than through a shell heredoc: an argument
    would put the whole document in the process table and make quoting a
    correctness problem."""
    local = staging / filename
    local.write_text(text)
    try:
        _copy(ops, "", local, destination)
    finally:
        local.unlink(missing_ok=True)


# --- sandbox operations, with the token scrubbed from every diagnostic -------


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _fail(ops: SandboxOps, token: str, what: str, exc: BaseException) -> BootstrapError:
    return BootstrapError(
        f"sandbox {ops.name}: {what} failed: {_redact(str(exc), token)}"
    )


def _exec(ops: SandboxOps, token: str, argv: Sequence[str]) -> str:
    try:
        return ops.exec(argv)
    except Exception as exc:
        raise _fail(ops, token, " ".join(argv), exc) from None


def _copy(ops: SandboxOps, token: str, source: Path, destination: str) -> None:
    try:
        ops.copy_in(source, destination)
    except Exception as exc:
        raise _fail(ops, token, f"copy to {destination}", exc) from None


def _home(ops: SandboxOps, token: str) -> str:
    """The agent user's home inside the VM. Asked rather than assumed: the
    Claude and Codex images do not have to agree on it, and a wrong guess puts
    the capability where the shim will not look for it."""
    home = _exec(ops, token, ["sh", "-lc", 'printf %s "$HOME"']).strip()
    if not home.startswith("/"):
        raise BootstrapError(
            f"sandbox {ops.name}: could not resolve the agent's home directory "
            f"(got {home!r}); the context client config has nowhere to go"
        )
    return home
