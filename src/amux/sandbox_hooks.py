"""Generate sandbox-local Claude and Codex hooks that call the amux shim.

A sandbox inherits none of the user's agent configuration, so without this an
agent inside a microVM never reports busy/idle/needs-input/dead and every roster
read about it is a guess. Bootstrap therefore writes the hooks itself.

It writes *only* the amux entries. The user's own host configuration is never
copied in — it names host paths and host helpers that do not exist in a VM.

Everything here is a pure function from existing configuration to merged
configuration, so the merge is tested against fixtures rather than a live
sandbox. Only the *file locations* need a real image to confirm; see
`AgentHooks.paths_are_assumed`.

Reference configuration
-----------------------
Both shapes are taken from verified working host configuration — the files amux
relies on today — not from documentation or memory.

Claude Code, `~/.claude/settings.json`; Codex, `~/.codex/hooks.json`. The
structure is the same in both:

    hooks:
      <EventName>: [ { matcher?: str,
                       hooks: [ {type: "command", command: str,
                                 timeout?: int, ...} ] } ]

The event *names* differ, so each agent gets its own map. Codex has no
`Notification`; its equivalent is `PermissionRequest`. Both reach every amux
kind, so **a sandboxed Codex is not structurally weaker than a sandboxed
Claude** — an earlier revision of this module claimed otherwise on the strength
of Codex's older single-slot `notify` key, which is a different, superseded
mechanism.

Two Codex-specific requirements that `hooks.json` alone does not satisfy:

- `config.toml` must carry `hooks = true` **under `[features]`**. It is not a
  top-level key. Writing `hooks.json` without it leaves the hooks inert.
- `config.toml` also grows a `[hooks.state]` table in which Codex records a
  `trusted_hash = "sha256:..."` per hook entry, keyed
  `<hooks.json path>:<event_snake_case>:<group>:<index>`. amux does **not**
  write those: what exactly is hashed is not documented, and a wrong hash is
  worse than an absent one. It means hook trust may need approval on first run,
  which is listed as an open question for the live smoke test — a headless
  sandbox cannot answer an approval prompt.

The older `notify` slot is kept as a fallback for a Codex too old to have hooks,
selected by version detection rather than assumed. See `codex_supports_hooks`.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: Marker used to recognise an amux-installed hook, so merging twice is a no-op.
HOOK_MARKER = "event emit"

#: Claude Code event -> amux event kind, from the verified host `settings.json`.
CLAUDE_EVENT_KINDS: dict[str, str] = {
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "Stop": "stop",
    "Notification": "notify",
    "SessionEnd": "exit",
}
#: Codex event -> amux event kind, from the verified host `hooks.json`
#: (codex-cli 0.146.0). `PermissionRequest` is Codex's `Notification`.
CODEX_EVENT_KINDS: dict[str, str] = {
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "Stop": "stop",
    "PermissionRequest": "notify",
    "SessionEnd": "exit",
}

#: Claude's `UserPromptSubmit` carries no matcher in the reference host file;
#: every Codex event does. Mirrored rather than reasoned about.
CLAUDE_UNMATCHED_EVENTS = frozenset({"UserPromptSubmit"})
CODEX_UNMATCHED_EVENTS: frozenset[str] = frozenset()

#: Seconds. A hook that cannot reach the host must not stall the agent.
HOOK_TIMEOUT = 10
#: Per-agent, per-event overrides where the agent imposes its own ceiling.
#: Codex caps SessionEnd at 3s and warns on every run when asked for more
#: ("clamping SessionEnd hook timeout to 3s"), observed on codex-cli 0.146.0.
#: Asking for what it will grant keeps that warning out of the agent's output.
HOOK_TIMEOUT_OVERRIDES: dict[tuple[str, str], int] = {("codex", "SessionEnd"): 3}

#: Where Codex's feature switch lives, and the table it lives in.
CODEX_FEATURES_TABLE = "features"
CODEX_HOOKS_FEATURE = "hooks"

#: Earliest Codex verified to support `hooks.json`. The real introduction may be
#: earlier; this is deliberately conservative, because guessing low would install
#: hooks that never fire, while guessing high only falls back to `notify` — which
#: still reports `stop` and says so.
CODEX_HOOKS_MIN_VERSION = (0, 146, 0)

CODEX_DISPATCH_PATH = "/usr/local/bin/amux-codex-notify"
#: The fallback slot fires when a turn ends, which is `stop`.
CODEX_NOTIFY_KIND = "stop"

ALL_KINDS = ("spawn", "busy", "stop", "notify", "exit")
#: `spawn` is amux's own — it is stamped by the host at grid creation, never by
#: an agent hook, so it is not something an adapter can be missing.
HOOK_SUPPLIED_KINDS = ("busy", "stop", "notify", "exit")


class HookMergeError(Exception):
    """Existing agent configuration could not be merged safely."""


@dataclass(frozen=True)
class AgentHooks:
    """Where and how one agent's hook configuration is written in a sandbox.

    `paths_are_assumed` records whether the *locations* have been checked against
    a real Docker image, as opposed to taken from documented defaults. Both were
    verified on 2026-08-05 against `docker/sandbox-templates` — see the fixtures
    README for what was observed and how to re-record it.
    """

    agent: str
    settings_relpath: str
    events: Mapping[str, str]
    unmatched_events: frozenset[str] = field(default_factory=frozenset)
    #: Config file that must switch hooks on before `settings_relpath` is read.
    enable_relpath: str | None = None
    paths_are_assumed: bool = True


#: Verified in `docker/sandbox-templates:claude-code-docker`, Claude Code
#: 2.1.221: runs as `agent` with `HOME=/home/agent`, and ships a
#: `~/.claude/settings.json` carrying UI preferences and no `hooks` key.
CLAUDE = AgentHooks(
    agent="claude",
    settings_relpath=".claude/settings.json",
    events=CLAUDE_EVENT_KINDS,
    unmatched_events=CLAUDE_UNMATCHED_EVENTS,
    paths_are_assumed=False,
)
#: Verified in `docker/sandbox-templates:codex-docker`, codex-cli 0.146.0: runs
#: as `agent` with `HOME=/home/agent`, ships a `~/.codex/config.toml` and *no*
#: `hooks.json`, so amux creates that file rather than merging into one.
CODEX = AgentHooks(
    agent="codex",
    settings_relpath=".codex/hooks.json",
    events=CODEX_EVENT_KINDS,
    unmatched_events=CODEX_UNMATCHED_EVENTS,
    enable_relpath=".codex/config.toml",
    paths_are_assumed=False,
)

HOOKS_BY_AGENT: dict[str, AgentHooks] = {CLAUDE.agent: CLAUDE, CODEX.agent: CODEX}


def hooks_for(agent: str) -> AgentHooks:
    try:
        return HOOKS_BY_AGENT[agent]
    except KeyError:
        raise HookMergeError(
            f"no sandbox hook adapter for agent '{agent}'; "
            f"supported: {', '.join(sorted(HOOKS_BY_AGENT))}"
        ) from None


def emit_command(shim: str, config_path: str, kind: str, agent: str) -> str:
    """The shell command a hook runs.

    `AMUX_CONTEXT_CONFIG` is set inline so a hook works even if it runs with a
    different `HOME` than bootstrap resolved. It is a path, not a secret — the
    capability stays inside the 0600 file this points at.
    """
    return (
        f"AMUX_CONTEXT_CONFIG={shlex.quote(config_path)} {shlex.quote(shim)} "
        f"event emit {kind} --agent {agent}"
    )


# --- the shared JSON hook document -------------------------------------------


def _already_installed(groups: Iterable[Any]) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")):
                return True
    return False


def merge_hook_settings(
    existing: dict | None, adapter: AgentHooks, *, shim: str, config_path: str
) -> dict:
    """Add amux's hooks to an agent's hook document.

    Template-owned settings survive: every key outside `hooks` is untouched, and
    within `hooks` amux only *appends its own group* to each event array. It
    never edits, reorders or replaces a group it did not write — appending to
    someone else's group would silently inherit their matcher, which for
    `PreToolUse` would mean the busy hook fires for one tool instead of all.

    Idempotent: an event that already carries an amux hook is left alone, so
    re-running bootstrap cannot stack duplicates.

    The cost of that, which bit during the live probe: re-running bootstrap will
    *not* update an amux hook whose command or timeout has since changed, because
    it sees the marker and skips. Sandboxes are created fresh, so this is fine
    today — but a hook already installed in a live sandbox has to be removed
    before a changed one will take.
    """
    if existing is not None and not isinstance(existing, dict):
        raise HookMergeError(
            f"{adapter.agent} hook settings must be a JSON object, "
            f"got {type(existing).__name__}"
        )
    merged = json.loads(json.dumps(existing or {}))  # deep copy, no shared state
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookMergeError(
            f"the 'hooks' key must be an object, got {type(hooks).__name__}"
        )
    for event, kind in adapter.events.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise HookMergeError(f"hooks.{event} must be an array, got {groups!r}")
        if _already_installed(groups):
            continue
        group: dict[str, Any] = {}
        if event not in adapter.unmatched_events:
            group["matcher"] = ""
        group["hooks"] = [
            {
                "type": "command",
                "command": emit_command(shim, config_path, kind, adapter.agent),
                "timeout": HOOK_TIMEOUT_OVERRIDES.get(
                    (adapter.agent, event), HOOK_TIMEOUT
                ),
            }
        ]
        groups.append(group)
    return merged


def merge_claude_settings(existing: dict | None, *, shim: str, config_path: str) -> dict:
    return merge_hook_settings(existing, CLAUDE, shim=shim, config_path=config_path)


def merge_codex_hooks(existing: dict | None, *, shim: str, config_path: str) -> dict:
    return merge_hook_settings(existing, CODEX, shim=shim, config_path=config_path)


def render_hook_settings(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


#: Kept as the historical name used elsewhere.
render_claude_settings = render_hook_settings


# --- Codex: switching hooks on in config.toml --------------------------------


def codex_hooks_enabled(config_text: str) -> bool:
    import tomllib

    try:
        document = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise HookMergeError(f"the Codex config.toml is not valid TOML: {exc}") from None
    features = document.get(CODEX_FEATURES_TABLE)
    return bool(isinstance(features, dict) and features.get(CODEX_HOOKS_FEATURE))


def enable_codex_hooks(config_text: str) -> str:
    """Ensure `hooks = true` under `[features]` in a Codex `config.toml`.

    `hooks.json` alone is inert without this, and the switch is **not** a
    top-level key — it lives in the `[features]` table, as on the verified host.

    An existing `hooks = ...` inside `[features]` is commented out rather than
    edited in place, so nothing is lost and a commented line cannot collide as a
    duplicate key. When there is no `[features]` table, one is *appended*: a new
    table header is safe at the end of a file, whereas a bare key would land in
    whichever table happens to be last.
    """
    if codex_hooks_enabled(config_text):
        return config_text
    lines = config_text.splitlines()
    setting = f"{CODEX_HOOKS_FEATURE} = true"
    header = _table_header_index(lines, CODEX_FEATURES_TABLE)
    if header is None:
        prefix = config_text if config_text.endswith("\n") or not config_text else config_text + "\n"
        return (
            prefix
            + f"\n# {CODEX_FEATURES_TABLE}.{CODEX_HOOKS_FEATURE}: enabled by amux "
            f"sandbox bootstrap; hooks.json is inert without it\n"
            f"[{CODEX_FEATURES_TABLE}]\n{setting}\n"
        )
    out = list(lines[: header + 1])
    out.append(f"# {CODEX_HOOKS_FEATURE} enabled by amux sandbox bootstrap")
    out.append(setting)
    index = header + 1
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("["):
            break  # left the [features] table; the rest is copied untouched
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if key == CODEX_HOOKS_FEATURE and not stripped.startswith("#"):
            out.append(f"# amux replaced this: {stripped}")
        else:
            out.append(lines[index])
        index += 1
    out.extend(lines[index:])
    return "\n".join(out) + "\n"


def _table_header_index(lines: list[str], table: str) -> int | None:
    for index, line in enumerate(lines):
        if line.strip() == f"[{table}]":
            return index
    return None


# --- Codex version detection -------------------------------------------------


def parse_codex_version(output: str) -> tuple[int, ...] | None:
    """The version tuple from `codex --version` output, or None.

    Accepts the verified `codex-cli 0.146.0` shape and anything else whose first
    dotted-numeric token is the version. None means "could not tell", which is
    treated as no hook support: falling back to `notify` still reports `stop` and
    says what is missing, whereas assuming hooks that are not there reports
    nothing at all.
    """
    for token in (output or "").replace(",", " ").split():
        parts = token.split(".")
        if len(parts) >= 2 and all(p.isdigit() for p in parts):
            return tuple(int(p) for p in parts)
    return None


def codex_supports_hooks(version_output: str) -> bool:
    version = parse_codex_version(version_output)
    return version is not None and version >= CODEX_HOOKS_MIN_VERSION


# --- the older single notify slot, kept as a fallback ------------------------


def _find_top_level_notify(text: str) -> tuple[int, int] | None:
    """Line span of a top-level `notify = [...]` assignment, or None.

    Only the region before the first table header is searched, because that is
    the only place a top-level key can live in TOML. The span may cover several
    lines, so brackets are counted rather than assuming one line.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            return None  # reached the first table: no top-level notify
        if stripped.startswith("#"):
            continue
        head = stripped.split("=", 1)
        if len(head) == 2 and head[0].strip() == "notify":
            depth = 0
            for end in range(index, len(lines)):
                depth += lines[end].count("[") - lines[end].count("]")
                if depth <= 0:
                    return index, end
            raise HookMergeError(
                "the existing 'notify' array in the Codex config is unterminated"
            )
    return None


def render_codex_dispatch(
    shim: str, config_path: str, previous: list[str] | None = None
) -> str:
    """A `/bin/sh` script for Codex's single `notify` slot.

    Only used when the image's Codex is too old for `hooks.json`. Codex passes
    the notification JSON as one argument, not on stdin, so it is piped into the
    shim, which reads hook payloads from stdin. Any previous notify command is
    chained with `exec`, so installing amux does not take the slot away from
    whatever already owned it.
    """
    lines = [
        "#!/bin/sh",
        "# Installed by amux sandbox bootstrap for a Codex without hooks.json.",
        "# Codex has a single notify slot, so this dispatches to amux and then",
        "# chains the previous consumer. The JSON arrives as $1; the amux shim",
        "# reads hook payloads on stdin.",
        "set -u",
        f'printf %s "${{1:-}}" | AMUX_CONTEXT_CONFIG={shlex.quote(config_path)} '
        f"{shlex.quote(shim)} event emit {CODEX_NOTIFY_KIND} --agent codex "
        ">/dev/null 2>&1 || true",
    ]
    if previous:
        lines.append("")
        lines.append("# chained from the value amux replaced")
        lines.append("exec " + " ".join(shlex.quote(part) for part in previous) + ' "$@"')
    return "\n".join(lines) + "\n"


def merge_codex_config(
    existing: str, *, dispatch_path: str = CODEX_DISPATCH_PATH
) -> tuple[str, list[str] | None]:
    """Point Codex's `notify` slot at the amux dispatch script (fallback path).

    Returns the new config text and whatever `notify` held before, which the
    caller bakes into the dispatch script so the previous consumer keeps firing.

    A replaced assignment is commented out rather than deleted: nothing is lost,
    an operator can see what happened, and a commented line cannot collide as a
    duplicate key. A new assignment is *prepended*, because appending would land
    inside whichever table happens to be last in the file.
    """
    if f'notify = ["{dispatch_path}"]' in existing:
        return existing, None  # idempotent
    replacement = f'notify = ["{dispatch_path}"]'
    span = _find_top_level_notify(existing)
    if span is None:
        prefix = "# notify: installed by amux sandbox bootstrap\n"
        return prefix + replacement + "\n" + existing, None

    lines = existing.splitlines()
    start, end = span
    previous = _parse_notify_value("\n".join(lines[start : end + 1]))
    commented = [
        "# amux sandbox bootstrap replaced this notify; it is chained from",
        f"# {dispatch_path} instead of being dropped.",
        *(f"# {line}" for line in lines[start : end + 1]),
    ]
    merged = [*lines[:start], *commented, replacement, *lines[end + 1 :]]
    return "\n".join(merged) + "\n", previous


def _parse_notify_value(assignment: str) -> list[str] | None:
    """The argv a `notify = [...]` assignment holds, via `tomllib`."""
    import tomllib

    try:
        value = tomllib.loads(assignment).get("notify")
    except tomllib.TOMLDecodeError as exc:
        raise HookMergeError(
            f"cannot read the existing Codex 'notify' value: {exc}"
        ) from None
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise HookMergeError(
            f"the existing Codex 'notify' must be an array of strings, got {value!r}"
        )
    return value


# --- what a sandbox agent's state can actually cover -------------------------


def state_coverage(agent: str, *, hooks_supported: bool = True) -> tuple[str, ...]:
    """The event kinds a sandboxed agent can report.

    Claude always reaches all four. Codex reaches all four through `hooks.json`
    and only `stop` through the older `notify` slot, so its coverage depends on
    the version actually present in the image — detected, never assumed.
    """
    adapter = hooks_for(agent)
    if adapter.agent == CODEX.agent and not hooks_supported:
        return (CODEX_NOTIFY_KIND,)
    return tuple(sorted(set(adapter.events.values())))


def missing_kinds(agent: str, *, hooks_supported: bool = True) -> tuple[str, ...]:
    """Kinds this sandbox cannot report. Non-empty means degraded integration:
    the caller must not present such an agent's state as authoritative."""
    covered = state_coverage(agent, hooks_supported=hooks_supported)
    return tuple(k for k in HOOK_SUPPLIED_KINDS if k not in covered)
