from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from amux import worktree
from amux.sandbox import HOOK_TRUST_FLAG
from amux.shared import SKILL_POINTER, STATE_DIR, AgentRequest

ROLE_NAME = re.compile(r"[a-z][a-z0-9-]*")
ROLE_AGENTS = ("claude", "codex")
ROLE_KEYS = ("description", "model", "effort", "subagents")
FRONTMATTER = "+++"
UNATTENDED_ARGS: dict[str, tuple[str, ...]] = {
    "claude": ("--disallowedTools", "SendFeedback,AskUserQuestion"),
    "codex": (
        HOOK_TRUST_FLAG,
        "-c",
        "tools.experimental_request_user_input.enabled=false",
        "-c",
        "check_for_update_on_startup=false",
    ),
}


class RoleError(ValueError):
    pass


@dataclass(frozen=True)
class Role:
    name: str
    prompt: str
    description: str = ""
    model: str = ""
    effort: str = ""
    subagents: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleLaunch:
    args: tuple[str, ...] = ()
    shell: str = ""


def project_root(cwd: str | None) -> str | None:
    return (worktree.repo_root(cwd) or cwd) if cwd else None


def role_dirs(repo: str | None) -> list[Path]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    dirs = [Path(repo) / ".amux" / "roles"] if repo else []
    return [*dirs, config / "amux" / "roles"]


def parse_role(name: str, text: str, path: Path) -> Role:
    lines = text.splitlines(keepends=True)
    meta: dict = {}
    body = text
    if lines and lines[0].strip() == FRONTMATTER:
        end = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == FRONTMATTER), None
        )
        if end is None:
            raise RoleError(f"{path}: frontmatter opened with '+++' is never closed")
        try:
            meta = tomllib.loads("".join(lines[1:end]))
        except tomllib.TOMLDecodeError as exc:
            raise RoleError(f"{path}: frontmatter is not valid TOML: {exc}") from exc
        body = "".join(lines[end + 1 :])
    unknown = sorted(set(meta) - set(ROLE_KEYS))
    if unknown:
        raise RoleError(
            f"{path}: unknown frontmatter key(s) {', '.join(unknown)}; "
            f"allowed: {', '.join(ROLE_KEYS)}"
        )
    for key in ("description", "model", "effort"):
        if not isinstance(meta.get(key, ""), str):
            raise RoleError(f"{path}: '{key}' must be a string")
    subagents = meta.get("subagents", [])
    if not isinstance(subagents, list) or not all(
        isinstance(s, str) and ROLE_NAME.fullmatch(s) for s in subagents
    ):
        raise RoleError(f"{path}: 'subagents' must be a list of role names")
    if name in subagents:
        raise RoleError(f"{path}: role '{name}' lists itself as a subagent")
    prompt = body.strip()
    if not prompt:
        raise RoleError(f"{path}: role '{name}' has no prompt below its frontmatter")
    return Role(
        name=name,
        prompt=prompt,
        description=meta.get("description", ""),
        model=meta.get("model", ""),
        effort=meta.get("effort", ""),
        subagents=tuple(subagents),
    )


def find_role(name: str, repo: str | None) -> Role:
    dirs = role_dirs(repo)
    for directory in dirs:
        path = directory / f"{name}.md"
        if path.is_file():
            return parse_role(name, path.read_text(), path)
    raise RoleError(
        f"no role '{name}': looked for {name}.md in " + ", ".join(str(d) for d in dirs)
    )


def load_roles(names: Iterable[str], repo: str | None) -> dict[str, Role]:
    loaded = {name: find_role(name, repo) for name in sorted(set(names))}
    for role in list(loaded.values()):
        for sub in role.subagents:
            if sub not in loaded:
                loaded[sub] = find_role(sub, repo)
            if loaded[sub].subagents:
                raise RoleError(
                    f"role '{sub}' is a subagent of '{role.name}' but lists "
                    "subagents of its own; a subagent cannot start subagents, so "
                    "give it a role file without 'subagents'"
                )
    return loaded


def resolve(
    requests: list[AgentRequest], cwd: str | None, runtime_kind: str
) -> list[AgentRequest]:
    wanted = {r.role for r in requests if r.role}
    if not wanted:
        return requests
    if runtime_kind != "host":
        raise RoleError(
            f"roles run on the host runtime only; --runtime {runtime_kind} "
            "cannot inject a role prompt yet"
        )
    loaded = load_roles(wanted, project_root(cwd))
    resolved = []
    for request in requests:
        role = loaded.get(request.role)
        if role is None:
            resolved.append(request)
            continue
        if request.agent not in ROLE_AGENTS:
            raise RoleError(
                f"role '{role.name}' runs on {'/'.join(ROLE_AGENTS)}, "
                f"not '{request.agent}'"
            )
        resolved.append(
            replace(
                request,
                model=request.model or role.model,
                effort=request.effort or role.effort,
            )
        )
    return resolved


def system_prompt(role: Role | None, parent: str = "") -> str:
    if role is None:
        return f"{SKILL_POINTER}\n"
    who = (
        f"You are the `{role.name}` subagent of the `{parent}` role."
        if parent
        else f"Your amux role is `{role.name}`."
    )
    return f"{who} {SKILL_POINTER}\n\n{role.prompt}\n"


def toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def cat_arg(prefix: str, path: Path) -> str:
    return f'"{prefix}$(cat {shlex.quote(str(path))})"'


def prompt_dir(workspace: str | None, task: str | None) -> Path:
    return STATE_DIR / "prompts" / (workspace or "_") / (task or "_")


def render_launch(
    agent: str,
    role: Role | None,
    subs: list[Role],
    name: str,
    *,
    workspace: str | None,
    task: str | None,
    trusted_dir: str | None,
) -> RoleLaunch:
    directory = prompt_dir(workspace, task)
    directory.mkdir(parents=True, exist_ok=True)
    parent = role.name if role else ""
    described = [
        (sub, sub.description or f"amux role {sub.name}", system_prompt(sub, parent))
        for sub in subs
    ]
    args = UNATTENDED_ARGS[agent]
    if agent == "claude":
        prompt = directory / f"{name}.md"
        prompt.write_text(system_prompt(role))
        args += ("--append-system-prompt-file", str(prompt))
        if not described:
            return RoleLaunch(args=args)
        agents = {
            sub.name: {
                "description": description,
                "prompt": text,
                **({"model": sub.model} if sub.model else {}),
            }
            for sub, description, text in described
        }
        path = directory / f"{name}.agents.json"
        path.write_text(json.dumps(agents, ensure_ascii=False, indent=2))
        return RoleLaunch(args=args, shell=f"--agents {cat_arg('', path)}")
    instructions = directory / f"{name}.developer.toml-str"
    instructions.write_text(toml_string(system_prompt(role)))
    if trusted_dir:
        trust = f'projects={{{toml_string(trusted_dir)}={{trust_level="trusted"}}}}'
        args += ("-c", trust)
    for sub, description, text in described:
        lines = [f"developer_instructions = {toml_string(text)}"]
        if sub.model:
            lines.append(f"model = {toml_string(sub.model)}")
        if sub.effort:
            lines.append(f"model_reasoning_effort = {toml_string(sub.effort)}")
        path = directory / f"{name}.{sub.name}.toml"
        path.write_text("\n".join(lines) + "\n")
        args += (
            "-c",
            f"agents.{sub.name}.description={toml_string(description)}",
            "-c",
            f"agents.{sub.name}.config_file={toml_string(str(path))}",
        )
    return RoleLaunch(
        args=args, shell=f"-c {cat_arg('developer_instructions=', instructions)}"
    )
