from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

HOOK_MARKER = "event emit"

CLAUDE_EVENT_KINDS: dict[str, str] = {
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "Stop": "stop",
    "Notification": "notify",
    "SessionEnd": "exit",
}
CODEX_EVENT_KINDS: dict[str, str] = {
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "Stop": "stop",
    "PermissionRequest": "notify",
    "SessionEnd": "exit",
}

CLAUDE_UNMATCHED_EVENTS = frozenset({"UserPromptSubmit"})
CODEX_UNMATCHED_EVENTS: frozenset[str] = frozenset()

HOOK_TIMEOUT = 10
HOOK_TIMEOUT_OVERRIDES: dict[tuple[str, str], int] = {("codex", "SessionEnd"): 3}

CODEX_FEATURES_TABLE = "features"
CODEX_HOOKS_FEATURE = "hooks"

CODEX_HOOKS_MIN_VERSION = (0, 146, 0)

CODEX_DISPATCH_PATH = "/usr/local/bin/amux-codex-notify"
CODEX_NOTIFY_KIND = "stop"

ALL_KINDS = ("spawn", "busy", "stop", "notify", "exit")
HOOK_SUPPLIED_KINDS = ("busy", "stop", "notify", "exit")


class HookMergeError(Exception):
    pass


@dataclass(frozen=True)
class AgentHooks:
    agent: str
    settings_relpath: str
    events: Mapping[str, str]
    unmatched_events: frozenset[str] = field(default_factory=frozenset)
    enable_relpath: str | None = None
    paths_are_assumed: bool = True
    skills_relpath: str = ".claude/skills"


CLAUDE = AgentHooks(
    agent="claude",
    settings_relpath=".claude/settings.json",
    events=CLAUDE_EVENT_KINDS,
    unmatched_events=CLAUDE_UNMATCHED_EVENTS,
    paths_are_assumed=False,
)
CODEX = AgentHooks(
    agent="codex",
    settings_relpath=".codex/hooks.json",
    events=CODEX_EVENT_KINDS,
    unmatched_events=CODEX_UNMATCHED_EVENTS,
    enable_relpath=".codex/config.toml",
    paths_are_assumed=False,
    skills_relpath=".codex/skills",
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
    return (
        f"AMUX_CONTEXT_CONFIG={shlex.quote(config_path)} {shlex.quote(shim)} "
        f"event emit {kind} --agent {agent}"
    )


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
    if existing is not None and not isinstance(existing, dict):
        raise HookMergeError(
            f"{adapter.agent} hook settings must be a JSON object, "
            f"got {type(existing).__name__}"
        )
    merged = json.loads(json.dumps(existing or {}))
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


def merge_claude_settings(
    existing: dict | None, *, shim: str, config_path: str
) -> dict:
    return merge_hook_settings(existing, CLAUDE, shim=shim, config_path=config_path)


def merge_codex_hooks(existing: dict | None, *, shim: str, config_path: str) -> dict:
    return merge_hook_settings(existing, CODEX, shim=shim, config_path=config_path)


def render_hook_settings(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"


render_claude_settings = render_hook_settings


def codex_hooks_enabled(config_text: str) -> bool:
    import tomllib

    try:
        document = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise HookMergeError(
            f"the Codex config.toml is not valid TOML: {exc}"
        ) from None
    features = document.get(CODEX_FEATURES_TABLE)
    return bool(isinstance(features, dict) and features.get(CODEX_HOOKS_FEATURE))


def enable_codex_hooks(config_text: str) -> str:
    if codex_hooks_enabled(config_text):
        return config_text
    lines = config_text.splitlines()
    setting = f"{CODEX_HOOKS_FEATURE} = true"
    header = _table_header_index(lines, CODEX_FEATURES_TABLE)
    if header is None:
        prefix = (
            config_text
            if config_text.endswith("\n") or not config_text
            else config_text + "\n"
        )
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
            break
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


def parse_codex_version(output: str) -> tuple[int, ...] | None:
    for token in (output or "").replace(",", " ").split():
        parts = token.split(".")
        if len(parts) >= 2 and all(p.isdigit() for p in parts):
            return tuple(int(p) for p in parts)
    return None


def codex_supports_hooks(version_output: str) -> bool:
    version = parse_codex_version(version_output)
    return version is not None and version >= CODEX_HOOKS_MIN_VERSION


def _find_top_level_notify(text: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            return None
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
        lines.append(
            "exec " + " ".join(shlex.quote(part) for part in previous) + ' "$@"'
        )
    return "\n".join(lines) + "\n"


def merge_codex_config(
    existing: str, *, dispatch_path: str = CODEX_DISPATCH_PATH
) -> tuple[str, list[str] | None]:
    if f'notify = ["{dispatch_path}"]' in existing:
        return existing, None
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


def state_coverage(agent: str, *, hooks_supported: bool = True) -> tuple[str, ...]:
    adapter = hooks_for(agent)
    if adapter.agent == CODEX.agent and not hooks_supported:
        return (CODEX_NOTIFY_KIND,)
    return tuple(sorted(set(adapter.events.values())))


def missing_kinds(agent: str, *, hooks_supported: bool = True) -> tuple[str, ...]:
    covered = state_coverage(agent, hooks_supported=hooks_supported)
    return tuple(k for k in HOOK_SUPPLIED_KINDS if k not in covered)
