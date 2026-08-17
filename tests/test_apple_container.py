"""The Apple `container` adapter.

Everything here runs against the `fake_container` fixture; the recorded argv
lists are the contract with an evolving external CLI, exactly as
`test_sandbox_adapter` pins the `sbx` surface.
"""

from __future__ import annotations

import shlex

import pytest

from amux import apple_container
from amux.sandbox import Resources
from amux.shared import AgentRequest


def _argv(**overrides) -> tuple[str, ...]:
    kwargs = dict(
        image="docker.io/library/node:22",
        resources=Resources(cpus=3, memory="6g"),
        repo="/repos/proj",
        workdir="/state/worktrees/ws/t0/alpha",
        request=AgentRequest("claude"),
    )
    kwargs.update(overrides)
    return apple_container.launch_argv("amux-ws-t0-alpha-abcd1234", **kwargs)


# --- launch command composition ---


def test_launch_argv_pins_the_whole_run_surface():
    argv = _argv()
    assert argv[:6] == (
        "run",
        "--interactive",
        "--tty",
        "--rm",
        "--name",
        "amux-ws-t0-alpha-abcd1234",
    )
    joined = " ".join(argv)
    assert "--cpus 3" in joined
    assert "--memory 6g" in joined
    assert "--volume /repos/proj:/repos/proj" in joined
    assert (
        "--volume /state/worktrees/ws/t0/alpha:/state/worktrees/ws/t0/alpha" in joined
    )
    assert "--workdir /state/worktrees/ws/t0/alpha" in joined
    # the image separates container flags from the agent's own argv
    tail = argv[argv.index("docker.io/library/node:22") + 1 :]
    assert tail == (
        "npx",
        "-y",
        "@anthropic-ai/claude-code",
        "--dangerously-skip-permissions",
    )


def test_launch_argv_mounts_and_workdir_use_host_paths():
    """The worktree's `.git` file points into the repo by absolute path, so
    both mounts must land at their host paths for git to work inside."""
    argv = _argv()
    volumes = [argv[i + 1] for i, a in enumerate(argv) if a == "--volume"]
    assert volumes == [
        "/repos/proj:/repos/proj",
        "/state/worktrees/ws/t0/alpha:/state/worktrees/ws/t0/alpha",
    ]


def test_launch_argv_carries_the_git_safe_directory_env():
    """The mounted repo is owned by the host user while the container runs as
    root; without safe.directory every git command inside refuses."""
    argv = _argv()
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "GIT_CONFIG_COUNT=1" in envs
    assert "GIT_CONFIG_KEY_0=safe.directory" in envs
    assert "GIT_CONFIG_VALUE_0=*" in envs


def test_launch_argv_passes_agent_credentials_as_bare_keys():
    """Bare `--env KEY` inherits from the pane shell when set and is silently
    skipped when unset, so credentials are never embedded in the command."""
    argv = _argv()
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "ANTHROPIC_API_KEY" in envs
    assert "CLAUDE_CODE_OAUTH_TOKEN" in envs
    codex = _argv(request=AgentRequest("codex"))
    codex_envs = [codex[i + 1] for i, a in enumerate(codex) if a == "--env"]
    assert "OPENAI_API_KEY" in codex_envs
    assert "ANTHROPIC_API_KEY" not in codex_envs


def test_launch_argv_marks_the_vm_as_a_sandbox_for_claude():
    """The default image runs as root, and claude refuses
    --dangerously-skip-permissions as root unless IS_SANDBOX=1 (measured live:
    the launch died with 'cannot be used with root/sudo privileges')."""
    argv = _argv()
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "IS_SANDBOX=1" in envs


def test_launch_argv_appends_model_and_effort_tuning():
    argv = _argv(request=AgentRequest("claude", model="opus", effort="high"))
    assert argv[-4:] == ("--model", "opus", "--effort", "high")


def test_launch_argv_refuses_agents_it_cannot_install():
    with pytest.raises(apple_container.ContainerError, match="not supported"):
        _argv(request=AgentRequest("bash"))


def test_git_identity_env_reads_the_repo_config(git_repo, git_run):
    git_run(git_repo, "config", "user.name", "Repo Owner")
    git_run(git_repo, "config", "user.email", "owner@example.test")
    env = apple_container.git_identity_env(str(git_repo))
    assert "GIT_AUTHOR_NAME=Repo Owner" in env
    assert "GIT_COMMITTER_NAME=Repo Owner" in env
    assert "GIT_AUTHOR_EMAIL=owner@example.test" in env
    assert "GIT_COMMITTER_EMAIL=owner@example.test" in env


def test_git_identity_env_is_empty_when_unconfigured(git_repo):
    assert apple_container.git_identity_env(str(git_repo)) == ()


def test_launch_command_is_shell_safe(git_repo, git_run):
    git_run(git_repo, "config", "user.name", "Repo Owner; rm -rf /")
    command = apple_container.launch_command(
        "amux-ws-t0-alpha-abcd1234",
        image="docker.io/library/node:22",
        resources=Resources(),
        repo=str(git_repo),
        workdir="/state/worktrees/ws/t0/alpha",
        request=AgentRequest("claude"),
    )
    assert command.startswith("container run ")
    assert shlex.split(command)[0] == "container"
    assert "GIT_AUTHOR_NAME=Repo Owner; rm -rf /" in shlex.split(command)


# --- CLI surface ---


def test_containers_parses_the_list_shape(fake_container):
    fake_container.respond_json(
        "list",
        payload=[
            {"configuration": {"id": "amux-a"}, "status": "running"},
            {"configuration": {"id": "amux-b"}, "status": "stopped"},
        ],
    )
    assert [apple_container._entry_name(c) for c in apple_container.containers()] == [
        "amux-a",
        "amux-b",
    ]
    assert apple_container.exists("amux-a")
    assert apple_container.find("amux-c") is None
    assert fake_container.called_with("list", "--all", "--format", "json")


def test_containers_rejects_an_unexpected_list_shape(fake_container):
    fake_container.respond_json("list", payload={"containers": []})
    with pytest.raises(apple_container.ContainerError, match="expected a list"):
        apple_container.containers()


def test_stop_tolerates_a_missing_container(fake_container):
    fake_container.respond(
        "stop",
        stderr='Error: internalError: "failed to stop container" '
        '(cause: "notFound: "container with ID amux-a not found"")',
        returncode=1,
    )
    apple_container.stop("amux-a")


def test_stop_raises_on_any_other_failure(fake_container):
    fake_container.respond("stop", stderr="Error: XPC connection error", returncode=1)
    with pytest.raises(apple_container.ContainerError, match="XPC"):
        apple_container.stop("amux-a")


def test_remove_is_always_forced(fake_container):
    """The container mounts the agent's host worktree, so it holds nothing
    that could be lost; refusing on a running container would only strand it."""
    fake_container.respond("delete")
    apple_container.remove("amux-a")
    assert fake_container.called_with("delete", "--force", "amux-a")


def test_remove_tolerates_a_missing_container(fake_container):
    fake_container.respond(
        "delete",
        stderr='Error: internalError: (cause: "notFound: not found")',
        returncode=1,
    )
    apple_container.remove("amux-a")


def test_a_missing_cli_names_the_install_and_the_way_out(no_sbx):
    with pytest.raises(apple_container.ContainerError) as err:
        apple_container.version()
    assert "brew install container" in str(err.value)
    assert "--runtime apple-container" in str(err.value)


# --- preflight ---


def _preflight(**overrides):
    kwargs = dict(
        agents=["claude"],
        repo="/repos/proj",
        resources=Resources(),
        image=apple_container.DEFAULT_IMAGE,
    )
    kwargs.update(overrides)
    return apple_container.preflight(**kwargs)


def _by_name(report):
    return {check.name: check for check in report.checks}


def test_preflight_passes_on_a_healthy_host(fake_container):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    checks = _by_name(_preflight())
    assert all(check.ok for check in checks.values()), checks
    assert checks["container"].detail == "container CLI version 1.2.2"


def test_preflight_fails_agents_it_cannot_install(fake_container):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    check = _by_name(_preflight(agents=["claude", "bash"]))["agents"]
    assert not check.ok
    assert "bash" in check.detail


def test_preflight_requires_a_repository(fake_container):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    check = _by_name(_preflight(repo=""))["repository"]
    assert not check.ok
    assert "worktrees" in check.remediation


def test_preflight_reports_bad_resources_as_a_check(fake_container):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    check = _by_name(_preflight(resources=Resources(cpus=0)))["resources"]
    assert not check.ok


def test_preflight_stops_at_a_missing_cli(no_sbx):
    checks = _by_name(_preflight())
    assert not checks["container"].ok
    assert checks["container"].remediation == "brew install container"
    assert "container-system" not in checks


def test_preflight_reports_a_stopped_system_service(fake_container):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond(
        "system",
        "status",
        stdout="apiserver is not running and not registered with launchd\n",
        returncode=1,
    )
    check = _by_name(_preflight())["container-system"]
    assert not check.ok
    assert "container system start" in check.remediation
