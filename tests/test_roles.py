from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from amux import core, events, roles, runtime, store, utils, worktree
from amux.shared import SKILL_POINTER, AgentRequest

WORKER = """+++
description = "plans and runs one task"
model = "opus"
effort = "high"
subagents = ["scout"]
+++
You are the worker. Plan, then execute.
"""

SCOUT = """+++
description = "looks things up with a fresh context"
model = "sonnet"
+++
Answer one question with sources.
"""


def write_role(root: Path, name: str, text: str) -> Path:
    path = root / ".amux" / "roles" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def user_roles(tmp_path, monkeypatch) -> Path:
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return config / "amux" / "roles"


@pytest.fixture
def project(git_repo, user_roles) -> Path:
    write_role(git_repo, "worker", WORKER)
    write_role(git_repo, "scout", SCOUT)
    return git_repo


def parse(*specs: str) -> list[AgentRequest]:
    return core.parse_agent_specs(list(specs), None, None)


def test_a_role_prefix_sets_the_role():
    assert parse("worker=claude") == [AgentRequest("claude", role="worker")]


def test_a_role_keeps_model_effort_and_count():
    assert (
        parse("supervisor=codex@gpt-5.6-sol/xhigh:2")
        == [
            AgentRequest(
                "codex", model="gpt-5.6-sol", effort="xhigh", role="supervisor"
            )
        ]
        * 2
    )


@pytest.mark.parametrize(
    "spec",
    ["FOO=1 claude", "claude --settings=x", "codex -c model_reasoning_effort=high"],
)
def test_an_equals_sign_that_is_not_a_role_prefix_stays_raw(spec):
    assert parse(spec) == [AgentRequest(spec)]


def test_a_lowercase_env_assignment_points_at_env():
    with pytest.raises(ValueError, match="env x=1 claude"):
        parse("x=1 claude")


def test_a_role_on_a_raw_command_is_an_error():
    with pytest.raises(ValueError, match="not the raw command 'bash'"):
        parse("worker=bash")


def test_a_role_without_an_agent_is_an_error():
    with pytest.raises(ValueError, match="needs an agent"):
        parse("worker=")


def test_frontmatter_and_body_are_split(tmp_path):
    role = roles.parse_role("worker", WORKER, tmp_path / "worker.md")
    assert role.description == "plans and runs one task"
    assert (role.model, role.effort, role.subagents) == ("opus", "high", ("scout",))
    assert role.prompt == "You are the worker. Plan, then execute."


def test_a_file_without_frontmatter_is_all_prompt(tmp_path):
    role = roles.parse_role("editor", "Review the report.\n", tmp_path / "editor.md")
    assert role.prompt == "Review the report."
    assert role.subagents == ()


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('+++\ncolor = "red"\n+++\nx\n', "unknown frontmatter key"),
        ('+++\nmodel = "opus"\nx\n', "never closed"),
        ("+++\nmodel = opus\n+++\nx\n", "not valid TOML"),
        ("+++\nmodel = 3\n+++\nx\n", "'model' must be a string"),
        ('+++\nsubagents = "scout"\n+++\nx\n', "list of role names"),
        ('+++\nsubagents = ["worker"]\n+++\nx\n', "lists itself"),
        ('+++\nmodel = "opus"\n+++\n\n', "has no prompt"),
    ],
)
def test_a_malformed_role_file_is_rejected(tmp_path, text, error):
    with pytest.raises(roles.RoleError, match=error):
        roles.parse_role("worker", text, tmp_path / "worker.md")


def test_the_repo_role_wins_over_the_user_role(project, user_roles):
    user_roles.mkdir(parents=True)
    (user_roles / "worker.md").write_text("user worker\n")
    (user_roles / "editor.md").write_text("user editor\n")
    assert roles.find_role("worker", str(project)).prompt.startswith(
        "You are the worker"
    )
    assert roles.find_role("editor", str(project)).prompt == "user editor"


def test_a_missing_role_names_every_place_it_looked(project, user_roles):
    with pytest.raises(roles.RoleError) as exc:
        roles.find_role("wroker", str(project))
    assert str(project / ".amux" / "roles") in str(exc.value)
    assert str(user_roles) in str(exc.value)


def test_role_defaults_fill_only_what_the_spec_left_empty(project):
    resolved = roles.resolve(
        [
            AgentRequest("claude", role="worker"),
            AgentRequest("claude", effort="low", role="worker"),
        ],
        str(project),
        "host",
    )
    assert resolved == [
        AgentRequest("claude", model="opus", effort="high", role="worker"),
        AgentRequest("claude", model="opus", effort="low", role="worker"),
    ]


def test_specs_without_roles_are_returned_untouched():
    requests = [AgentRequest("claude"), AgentRequest("echo hi")]
    assert roles.resolve(requests, None, "docker-sandbox") is requests


def test_roles_are_refused_off_the_host(project):
    with pytest.raises(roles.RoleError, match="host runtime only"):
        roles.resolve(
            [AgentRequest("claude", role="worker")], str(project), "docker-sandbox"
        )


def test_a_subagent_with_subagents_of_its_own_fails_before_spawn(project):
    write_role(project, "worker", WORKER.replace('["scout"]', '["scout", "reviewer"]'))
    write_role(project, "reviewer", '+++\nsubagents = ["scout"]\n+++\nReview it.\n')
    with pytest.raises(roles.RoleError, match="lists subagents of its own"):
        roles.resolve([AgentRequest("claude", role="worker")], str(project), "host")


def test_a_pane_role_may_list_subagents_that_are_also_panes(project):
    requests = [
        AgentRequest("claude", role="worker"),
        AgentRequest("claude", role="scout"),
    ]
    assert len(roles.resolve(requests, str(project), "host")) == 2


def test_a_missing_subagent_fails_before_spawn(git_repo, user_roles):
    write_role(git_repo, "worker", WORKER)
    with pytest.raises(roles.RoleError, match="no role 'scout'"):
        roles.resolve([AgentRequest("claude", role="worker")], str(git_repo), "host")


def prepare(agent: str, role: str, cwd: Path) -> runtime.Launch:
    (launch,) = runtime.HostRuntime().prepare(
        [runtime.PaneSpec("%1", agent, "alpha", role=role)],
        workspace=None,
        task=None,
        cwd=str(cwd),
    )
    return launch


def shell_argv(command: str, base: str) -> list[str]:
    assert command.startswith(base)
    dump = f"{sys.executable} -c 'import json,sys; print(json.dumps(sys.argv[1:]))'"
    out = subprocess.run(
        ["bash", "-c", dump + command[len(base) :]],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def test_a_claude_role_gets_its_prompt_file_and_subagents(project):
    launch = prepare("claude", "worker", project)
    argv = shell_argv(launch.keys[-1], runtime.AGENT_COMMANDS["claude"])
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "SendFeedback,AskUserQuestion"
    prompt = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
    assert prompt.startswith("Your amux role is `worker`.")
    assert SKILL_POINTER in prompt and "Plan, then execute." in prompt
    agents = json.loads(argv[argv.index("--agents") + 1])
    assert agents["scout"]["model"] == "sonnet"
    assert agents["scout"]["description"] == "looks things up with a fresh context"
    assert "subagent of the `worker` role" in agents["scout"]["prompt"]


def test_a_codex_role_gets_developer_instructions_and_agent_roles(project):
    write_role(
        project,
        "worker",
        WORKER.replace('model = "opus"\n', "") + 'Quote " and \\ and ✓\n',
    )
    launch = prepare("codex", "worker", project)
    argv = shell_argv(launch.keys[-1], runtime.AGENT_COMMANDS["codex"])
    overrides = [argv[i + 1] for i, word in enumerate(argv) if word == "-c"]
    instructions = next(o for o in overrides if o.startswith("developer_instructions="))
    text = tomllib.loads("x = " + instructions.split("=", 1)[1])["x"]
    assert text.startswith("Your amux role is `worker`.")
    assert 'Quote " and \\ and ✓' in text
    config_file = next(
        o for o in overrides if o.startswith("agents.scout.config_file=")
    )
    sub = tomllib.loads(
        Path(tomllib.loads("x = " + config_file.split("=", 1)[1])["x"]).read_text()
    )
    assert sub["model"] == "sonnet"
    assert "subagent of the `worker` role" in sub["developer_instructions"]
    assert any(o.startswith("agents.scout.description=") for o in overrides)
    assert "--dangerously-bypass-hook-trust" in argv
    assert "tools.experimental_request_user_input.enabled=false" in overrides
    assert "check_for_update_on_startup=false" in overrides
    trust = next(o for o in overrides if o.startswith("projects="))
    assert tomllib.loads(trust)["projects"][str(project)] == {"trust_level": "trusted"}


def test_the_toml_string_escapes_what_toml_forbids():
    text = "tab\there\x7f\x01 ☃ 🙂"
    assert tomllib.loads("x = " + roles.toml_string(text))["x"] == text


def test_prompts_land_in_the_isolated_state_dir(project, isolate_state):
    launch = prepare("claude", "worker", project)
    assert str(isolate_state / "prompts") in launch.keys[-1]


@pytest.fixture
def repo(git_repo) -> str:
    return str(git_repo)


def commit_in(path: str, name: str, git_run, text: str | None = None) -> None:
    (Path(path) / name).write_text(name if text is None else text)
    git_run(Path(path), "add", name)
    git_run(Path(path), "commit", "-qm", f"add {name}")


def test_base_starts_a_new_task_on_the_given_ref(repo, git_run):
    git_run(Path(repo), "branch", "record")
    commit_in(repo, "later.txt", git_run)
    host = runtime.HostRuntime(base="record")
    host.preflight([], workspace="ws", task="t1", cwd=repo)
    assert host.base_commit == git_run(Path(repo), "rev-parse", "record")
    integration = worktree.setup_task_integration(
        repo, "ws", "t1", base=host.base_commit
    )
    assert integration.base_ref == host.base_commit
    assert integration.base_ref != git_run(Path(repo), "rev-parse", "HEAD")


def test_base_refuses_an_unknown_ref_and_an_existing_task(repo):
    with pytest.raises(worktree.WorktreeError, match="not a commit"):
        runtime.HostRuntime(base="nope").preflight(
            [], workspace="ws", task="t1", cwd=repo
        )
    worktree.setup_task_integration(repo, "ws", "t1")
    with pytest.raises(worktree.WorktreeError, match="already exists"):
        runtime.HostRuntime(base="HEAD").preflight(
            [], workspace="ws", task="t1", cwd=repo
        )


def spawn_task(repo: str, task: str, git_run, files: dict[str, str]) -> None:
    paths = worktree.setup_task(
        repo, "ws", task, [("%1", AgentRequest("claude"), "alpha")]
    )
    for name, text in files.items():
        commit_in(paths["%1"], name, git_run, text)
    (result,) = worktree.integrate("ws", task)
    assert result.ok, result.error


def test_into_creates_the_target_and_merges_each_task(repo, git_run):
    spawn_task(repo, "t1", git_run, {"a.txt": "a"})
    first = worktree.integrate_into("ws", "t1", "amux/ws/record")
    assert (first.ok, first.commits) == (True, 2)
    spawn_task(repo, "t2", git_run, {"b.txt": "b"})
    second = worktree.integrate_into("ws", "t2", "amux/ws/record")
    assert second.ok
    tree = git_run(Path(repo), "ls-tree", "--name-only", "amux/ws/record").split()
    assert {"a.txt", "b.txt"} <= set(tree)
    parents = git_run(Path(repo), "log", "-1", "--format=%P", "amux/ws/record").split()
    assert len(parents) == 2


def test_into_twice_is_a_no_op(repo, git_run):
    spawn_task(repo, "t1", git_run, {"a.txt": "a"})
    worktree.integrate_into("ws", "t1", "record")
    before = git_run(Path(repo), "rev-parse", "record")
    again = worktree.integrate_into("ws", "t1", "record")
    assert (again.ok, again.commits) == (True, 0)
    assert git_run(Path(repo), "rev-parse", "record") == before


def test_into_records_a_task_note(repo, git_run):
    spawn_task(repo, "t1", git_run, {"a.txt": "a"})
    worktree.integrate_into("ws", "t1", "record", pane="%9")
    texts = [n["text"] for n in store.query_notes(workspace="ws", task="t1")]
    assert any("into record" in t for t in texts)


def test_into_reports_a_conflict_and_leaves_the_target_alone(repo, git_run):
    spawn_task(repo, "t1", git_run, {"same.txt": "one"})
    worktree.integrate_into("ws", "t1", "record")
    spawn_task(repo, "t2", git_run, {"same.txt": "two"})
    before = git_run(Path(repo), "rev-parse", "record")
    result = worktree.integrate_into("ws", "t2", "record")
    assert not result.ok and "same.txt" in result.error
    assert git_run(Path(repo), "rev-parse", "record") == before


def test_into_reports_a_target_that_moved_mid_merge(repo, git_run, monkeypatch):
    spawn_task(repo, "t1", git_run, {"a.txt": "a"})
    real = worktree._git  # noqa: SLF001

    def racing(path, *args, check=True):
        if args[:1] == ("update-ref",) and len(args) == 4 and args[3]:
            return subprocess.CompletedProcess(args, 1, "", "lock mismatch")
        return real(path, *args, check=check)

    monkeypatch.setattr(worktree, "_git", racing)
    result = worktree.integrate_into("ws", "t1", "record")
    assert not result.ok and "moved during the merge" in result.error


def test_into_refuses_a_checked_out_branch_and_the_main_line(repo, git_run):
    spawn_task(repo, "t1", git_run, {"a.txt": "a"})
    with pytest.raises(worktree.WorktreeError, match="checked out"):
        worktree.integrate_into("ws", "t1", "main")
    git_run(Path(repo), "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(worktree.WorktreeError, match="left to a human"):
        worktree.integrate_into("ws", "t1", "main")


@pytest.fixture
def team(monkeypatch, make_facts):
    facts = {
        "%1": make_facts(workspace="ws", task="t1", role="worker"),
        "%2": make_facts(workspace="ws", task="t1", role="supervisor"),
        "%3": make_facts(workspace="ws", task="t2", role="worker"),
        "%4": make_facts(workspace="ws", task="t2", role="worker"),
        "%5": make_facts(workspace="other", task="t1", role="supervisor"),
    }
    monkeypatch.setattr(events, "pane_facts_by_id", lambda socket=None: facts)
    return SimpleNamespace(socket_name="amux-root")


def test_send_role_finds_the_partner_in_the_senders_task(team):
    assert core.pane_for_role(team, "%1", "supervisor") == "%2"
    assert core.pane_for_role(team, "%2", "worker") == "%1"


def test_send_role_needs_exactly_one_match(team):
    with pytest.raises(ValueError, match="no 'editor'"):
        core.pane_for_role(team, "%1", "editor")
    with pytest.raises(ValueError, match="2 'worker'"):
        core.pane_for_role(team, "%2", "worker", task="t2")


def test_ctx_shows_the_role_next_to_the_agent():
    me = {
        "name": "alpha",
        "agent": "claude",
        "role": "worker",
        "label": "r0c0",
        "pane": "%1",
        "task": "t1",
        "workspace": "ws",
        "state": "idle",
        "cwd": "/w",
        "last_event": None,
    }
    mate = {
        **me,
        "name": "beta",
        "role": "",
        "agent": "codex",
        "label": "r0c1",
        "pane": "%2",
    }
    lines = utils.context_to_string(
        {"self": me, "team": [{"task": "t1", "agents": [me, mate]}]}
    )
    assert lines[0].startswith("you: alpha  worker=claude @r0c0 %1")
    assert any(
        "beta" in line and "codex" in line and "=codex" not in line for line in lines
    )


def test_a_brief_is_the_first_prompt_and_makes_the_pane_unattended(project, tmp_path):
    brief = tmp_path / "brief.md"
    text = '--- survey\n- Quote " and $HOME stay literal.'
    brief.write_text(text + "\n")
    host = runtime.HostRuntime(brief=str(brief))
    host.preflight(
        [AgentRequest("claude"), AgentRequest("codex")],
        workspace="ws",
        task="t1",
        cwd=str(project),
    )
    launches = host.prepare(
        [
            runtime.PaneSpec("%1", "claude", "alpha", role="worker"),
            runtime.PaneSpec("%2", "claude", "beta"),
            runtime.PaneSpec("%3", "codex", "gamma"),
        ],
        workspace="ws",
        task="t1",
        cwd=str(project),
    )
    brief.write_text("changed after spawn\n")
    for launch in launches:
        agent = "codex" if launch.pane == "%3" else "claude"
        argv = shell_argv(launch.keys[-1], runtime.AGENT_COMMANDS[agent])
        assert argv[-2:] == ["--", text]
        assert launch.bootstrap == ""
        assert set(roles.UNATTENDED_ARGS[agent]) <= set(argv)
    plain = shell_argv(launches[1].keys[-1], runtime.AGENT_COMMANDS["claude"])
    pointer = Path(plain[plain.index("--append-system-prompt-file") + 1]).read_text()
    assert pointer == SKILL_POINTER + "\n"
    codex = shell_argv(launches[2].keys[-1], runtime.AGENT_COMMANDS["codex"])
    instructions = next(a for a in codex if a.startswith("developer_instructions="))
    assert (
        tomllib.loads("x = " + instructions.split("=", 1)[1])["x"]
        == SKILL_POINTER + "\n"
    )


def test_a_brief_is_refused_for_raw_commands_and_missing_files(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("go\n")
    with pytest.raises(ValueError, match="raw command"):
        runtime.HostRuntime(brief=str(brief)).preflight(
            [AgentRequest("claude"), AgentRequest("bash")],
            workspace="ws",
            task="t1",
            cwd=None,
        )
    with pytest.raises(ValueError, match="is not a file"):
        runtime.HostRuntime(brief=str(tmp_path / "nope.md")).preflight(
            [AgentRequest("claude")], workspace="ws", task="t1", cwd=None
        )
