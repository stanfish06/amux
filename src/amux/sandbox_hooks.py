"""Generate sandbox-local Claude and Codex hooks that call the amux shim.

A sandbox inherits none of the user's agent configuration, so without this an
agent inside a microVM never reports busy/idle/needs-input/dead and every
roster read about it is a guess. Bootstrap therefore writes the hooks itself.

It writes *only* the amux entries. The user's own host configuration is never
copied in — it names host paths, host tools, and in the reference host config
below, host notification helpers that do not exist in a VM.

Everything here is a pure function from existing configuration text to merged
configuration text, so the merge is tested against fixtures rather than against
a live sandbox. Only the *file locations* need a real image to confirm; see
`AgentHooks.paths_are_assumed`.

Reference host configuration
----------------------------
The Claude shape is taken from a verified working host `settings.json`, which is
the configuration amux relies on today:

    hooks:
      <EventName>: [ { matcher?: str,
                       hooks: [ {type: "command", command: str,
                                 timeout?: int, async?: bool} ] } ]

with `UserPromptSubmit`/`PreToolUse` -> busy, `Stop` -> stop,
`Notification` -> notify, `SessionEnd` -> exit. That host file also carries
several unrelated hooks in the same event arrays, which is exactly the case the
merge has to preserve: amux appends its own match-all group and never edits or
replaces a group it did not write.

Codex is structurally weaker and this is a real limitation, not an oversight.
`~/.codex/config.toml` has a single top-level `notify` array — one slot, one
consumer, fired when a turn ends. There is no per-tool or per-notification
event. So a Codex sandbox can report `stop` and nothing else; `busy`, `notify`
and `exit` have no source. `codex_state_coverage()` states that plainly so the
runtime can report degraded integration rather than implying live state it does
not have. To keep the single slot shareable, amux installs a small dispatch
script and chains any previous value through it.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Marker used to recognise an amux-installed hook, so merging twice is a no-op.
HOOK_MARKER = "event emit"

#: Claude Code event -> amux event kind, mirroring the verified host config.
CLAUDE_EVENT_KINDS: dict[str, str] = {
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "Stop": "stop",
    "Notification": "notify",
    "SessionEnd": "exit",
}
#: `UserPromptSubmit` takes no matcher; the rest carry an empty match-all one,
#: as in the reference host file.
CLAUDE_UNMATCHED_EVENTS = frozenset({"UserPromptSubmit"})
#: Seconds. A hook that cannot reach the host must not stall the agent.
CLAUDE_HOOK_TIMEOUT = 10

CODEX_DISPATCH_PATH = "/usr/local/bin/amux-codex-notify"
#: Codex fires its single notify hook when a turn ends, which is `stop`.
CODEX_NOTIFY_KIND = "stop"


class HookMergeError(Exception):
    """Existing agent configuration could not be merged safely."""


@dataclass(frozen=True)
class AgentHooks:
    """Where one agent's hook configuration lives inside a sandbox.

    `paths_are_assumed` is the honest part: the formats below come from verified
    working configuration, but the *locations* inside Docker's agent images were
    not inspectable when this was written (`sbx policy init` had not been run, so
    no sandbox could be created). They are the documented defaults. Re-record
    them against a live image and flip the flag; nothing else has to change.
    """

    agent: str
    settings_relpath: str
    format: str
    paths_are_assumed: bool = True
    extra_files: tuple[str, ...] = field(default_factory=tuple)


CLAUDE = AgentHooks(
    agent="claude", settings_relpath=".claude/settings.json", format="json"
)
CODEX = AgentHooks(
    agent="codex",
    settings_relpath=".codex/config.toml",
    format="toml",
    extra_files=(CODEX_DISPATCH_PATH,),
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


# --- Claude: JSON settings ----------------------------------------------------


def _already_installed(groups: Iterable[Any]) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks") or []:
            if isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")):
                return True
    return False


def merge_claude_settings(existing: dict | None, *, shim: str, config_path: str) -> dict:
    """Add amux's hooks to a Claude `settings.json` document.

    Template-owned settings survive: every key outside `hooks` is untouched, and
    within `hooks` amux only *appends its own group* to each event array. It
    never edits, reorders or replaces a group it did not write — appending to
    someone else's group would silently inherit their matcher, which for
    `PreToolUse` would mean the busy hook fires for one tool instead of all.

    Idempotent: a document that already carries an amux hook for an event is
    left alone, so re-running bootstrap cannot stack duplicates.
    """
    if existing is not None and not isinstance(existing, dict):
        raise HookMergeError(
            f"Claude settings must be a JSON object, got {type(existing).__name__}"
        )
    merged = json.loads(json.dumps(existing or {}))  # deep copy, no shared state
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookMergeError(
            f"the 'hooks' key must be an object, got {type(hooks).__name__}"
        )
    for event, kind in CLAUDE_EVENT_KINDS.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise HookMergeError(f"hooks.{event} must be an array, got {groups!r}")
        if _already_installed(groups):
            continue
        group: dict[str, Any] = {}
        if event not in CLAUDE_UNMATCHED_EVENTS:
            group["matcher"] = ""
        group["hooks"] = [
            {
                "type": "command",
                "command": emit_command(shim, config_path, kind, "claude"),
                "timeout": CLAUDE_HOOK_TIMEOUT,
            }
        ]
        groups.append(group)
    return merged


def render_claude_settings(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


# --- Codex: one TOML notify slot ---------------------------------------------


def _find_top_level_notify(text: str) -> tuple[int, int] | None:
    """Line span of a top-level `notify = [...]` assignment, or None.

    Only the region before the first table header is searched, because that is
    the only place a top-level key can live in TOML. The span may cover several
    lines, so brackets are counted rather than assuming one line.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[["):
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
    """A `/bin/sh` script for Codex's single notify slot.

    Codex passes the notification JSON as one argument, not on stdin, so it is
    piped into the shim, which reads hook payloads from stdin. Any previous
    notify command is chained afterwards with `exec`, so installing amux does not
    take the slot away from whatever already owned it.
    """
    lines = [
        "#!/bin/sh",
        "# Installed by amux sandbox bootstrap. Codex has a single notify slot,",
        "# so this dispatches to amux and then chains the previous consumer.",
        "# The notification JSON arrives as $1; the amux shim reads it on stdin.",
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
    """Point Codex's `notify` slot at the amux dispatch script.

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

ALL_KINDS = ("spawn", "busy", "stop", "notify", "exit")


def claude_state_coverage() -> tuple[str, ...]:
    """The event kinds a sandboxed Claude reports. `spawn` is amux's own."""
    return tuple(sorted(set(CLAUDE_EVENT_KINDS.values())))


def codex_state_coverage() -> tuple[str, ...]:
    """The event kinds a sandboxed Codex reports — only `stop`.

    Codex's single `notify` slot fires at end of turn and there is no per-tool or
    per-notification event, so `busy`, `notify` and `exit` have no source inside
    the VM. Callers must surface this as degraded integration rather than
    presenting a Codex sandbox's resolved state as being as live as Claude's.
    """
    return (CODEX_NOTIFY_KIND,)


def missing_kinds(agent: str) -> tuple[str, ...]:
    covered = claude_state_coverage() if agent == "claude" else codex_state_coverage()
    return tuple(k for k in ALL_KINDS if k not in covered and k != "spawn")
