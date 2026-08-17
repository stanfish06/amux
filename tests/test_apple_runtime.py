"""The apple-container runtime: host worktrees, containerized execution.

The design under test: `prepare` builds the same per-agent worktrees as the
host runtime and only composes a `container run` command for the pane — amux
itself never creates a container, so there is nothing to roll back and the
work always lives on host branches. Lifecycle (`stop_task`, `clean_task`)
is what actually talks to the CLI, against the `fake_container` fixture.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from amux import apple_container, cli, runtime, store, worktree
from amux.shared import AgentRequest


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


@pytest.fixture
def repo(git_repo):
    return git_repo


def prepare(repo, *pane_specs: tuple[str, str, str]) -> list[runtime.Launch]:
    return runtime.AppleContainerRuntime().prepare(
        specs(*pane_specs), workspace="ws", task="t0", cwd=str(repo)
    )


# --- prepare ---


def test_prepare_builds_host_worktrees_and_container_launches(repo):
    launches = prepare(repo, ("%1", "claude", "alpha"), ("%2", "codex", "beta"))
    for launch, name in zip(launches, ("alpha", "beta"), strict=True):
        assert launch.cwd.endswith(f"/worktrees/ws/t0/{name}")
        assert Path(launch.cwd).is_dir()
        cd, run_script = launch.keys
        assert cd == f"cd {launch.cwd}"
        # The composed command runs from a script: typed inline it exceeds a
        # kilobyte, which a freshly created pane's shell chops and reorders.
        script = shlex.split(run_script)
        assert script[0] == "sh"
        argv = shlex.split(Path(script[1]).read_text().splitlines()[-1])
        assert argv[:3] == ["exec", "container", "run"]
        assert f"{repo}:{repo}" in argv
        assert f"{launch.cwd}:{launch.cwd}" in argv
        assert argv[argv.index("--workdir") + 1] == launch.cwd


def test_prepare_records_rows_on_the_apple_runtime(repo):
    prepare(repo, ("%1", "claude", "alpha"))
    (row,) = store.worktrees_for("ws", "t0")
    assert row["runtime"] == "apple-container"
    assert row["runtime_status"] == "running"
    assert row["sandbox_name"] == apple_container.container_name(
        "ws", "t0", "alpha", str(repo)
    )
    # the host half is untouched: real path, real branch
    assert row["path"].endswith("/worktrees/ws/t0/alpha")
    assert row["branch"] == "amux/ws/t0/alpha"


def test_prepare_sends_no_bootstrap_into_the_container(repo):
    """The container has no amux client and no installed skill; pointing the
    agent at amux vocabulary it cannot use would be worse than silence."""
    (launch,) = prepare(repo, ("%1", "claude", "alpha"))
    assert launch.bootstrap == ""
    script = Path(shlex.split(launch.keys[1])[1]).read_text()
    assert "--append-system-prompt" not in script


def test_prepare_requires_a_git_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(apple_container.ContainerError, match="not a git repository"):
        prepare(plain, ("%1", "claude", "alpha"))


def test_prepare_requires_workspace_task_and_path(repo):
    with pytest.raises(apple_container.ContainerError, match="workspace, task"):
        runtime.AppleContainerRuntime().prepare(
            specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=None
        )


def test_rollback_has_nothing_to_release(repo):
    r = runtime.AppleContainerRuntime()
    prepare(repo, ("%1", "claude", "alpha"))
    assert r.rollback() == []


# --- preflight ---


def test_preflight_raises_with_every_failed_check(no_sbx):
    with pytest.raises(apple_container.ContainerError) as err:
        runtime.AppleContainerRuntime().preflight(
            [AgentRequest("bash")], workspace="ws", task="t0", cwd=None
        )
    text = str(err.value)
    assert text.startswith("apple-container preflight failed:")
    assert "agents" in text
    assert "repository" in text
    assert "container" in text


def test_preflight_passes_on_a_healthy_host(fake_container, repo):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    runtime.AppleContainerRuntime().preflight(
        [AgentRequest("claude")], workspace="ws", task="t0", cwd=str(repo)
    )


# --- lifecycle ---


def test_stop_task_removes_the_container_and_records_it(fake_container, repo):
    prepare(repo, ("%1", "claude", "alpha"))
    name = apple_container.container_name("ws", "t0", "alpha", str(repo))
    fake_container.respond("stop")
    assert runtime.stop_task("ws", "t0") == [name]
    assert fake_container.called_with("stop", name)
    (row,) = store.worktrees_for("ws", "t0")
    # --rm containers vanish when stopped (measured on container 1.2.2), so
    # "removed" is the truthful record, not "stopped".
    assert row["runtime_status"] == "removed"
    # A removed row is invisible to clean_task, so the launch script must go
    # now or it leaks forever.
    assert not Path(apple_container.launch_script_path(name)).exists()


def test_stop_task_treats_a_missing_container_as_already_stopped(
    fake_container, repo
):
    prepare(repo, ("%1", "claude", "alpha"))
    fake_container.respond(
        "stop", stderr='(cause: "notFound: not found")', returncode=1
    )
    assert len(runtime.stop_task("ws", "t0")) == 1
    (row,) = store.worktrees_for("ws", "t0")
    assert row["runtime_status"] == "removed"


def test_clean_task_deletes_containers_without_a_preservation_pass(
    fake_container, repo
):
    prepare(repo, ("%1", "claude", "alpha"))
    name = apple_container.container_name("ws", "t0", "alpha", str(repo))
    assert Path(apple_container.launch_script_path(name)).exists()
    fake_container.respond("delete")
    assert runtime.clean_task("ws", "t0") == [name]
    assert fake_container.called_with("delete", "--force", name)
    (row,) = store.worktrees_for("ws", "t0")
    assert row["runtime_status"] == "removed"
    assert not Path(apple_container.launch_script_path(name)).exists()
    # no git traffic, no sbx traffic: the work is already on the host
    assert all(call[0] == "delete" for call in fake_container.calls)


def test_clean_task_reports_a_container_it_could_not_remove(fake_container, repo):
    prepare(repo, ("%1", "claude", "alpha"))
    fake_container.respond("delete", stderr="Error: XPC connection error", returncode=1)
    from amux import sandbox

    with pytest.raises(sandbox.SandboxError, match="could not be removed"):
        runtime.clean_task("ws", "t0")
    (row,) = store.worktrees_for("ws", "t0")
    assert row["runtime_status"] == "running"


def test_sandbox_tasks_lists_apple_tasks_for_workspace_cleanup(fake_container, repo):
    prepare(repo, ("%1", "claude", "alpha"))
    assert runtime.sandbox_tasks("ws") == ["t0"]


def test_integrate_merges_an_apple_row_from_the_host_branch(repo, git_run):
    """Work committed inside the container lands on the host branch through
    the mount, so integration is the plain host path — no fetch, no remote."""
    (launch,) = prepare(repo, ("%1", "claude", "alpha"))
    wt = Path(launch.cwd)
    (wt / "change.txt").write_text("from inside the container\n")
    git_run(wt, "add", "change.txt")
    git_run(wt, "commit", "-qm", "agent work")
    (result,) = worktree.integrate("ws", "t0")
    assert result.ok, result.error
    assert result.commits == 1


# --- CLI wiring ---


def _args(**overrides):
    class Args:
        runtime = "apple-container"
        cpus = None
        memory = None
        share_skills = False
        context_port = None
        image = None

    args = Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_the_cli_builds_the_apple_runtime_with_the_flags_as_given():
    chosen = cli._resolve_runtime(_args(cpus=4, memory="8g", image="node:20"))
    assert isinstance(chosen, runtime.AppleContainerRuntime)
    assert chosen.config.image == "node:20"
    assert chosen.config.resources.cpus == 4
    assert chosen.config.resources.memory == "8g"


def test_the_cli_defaults_the_image(capsys):
    chosen = cli._resolve_runtime(_args())
    assert chosen.config.image == apple_container.DEFAULT_IMAGE


@pytest.mark.parametrize(
    ("argv", "rejected"),
    [
        (["spw", "ws", "--image", "node:20"], "--image"),
        (
            ["spw", "ws", "--runtime", "apple-container", "--share-skills"],
            "--share-skills",
        ),
        (
            ["spw", "ws", "--runtime", "apple-container", "--context-port", "5000"],
            "--context-port",
        ),
        (["spw", "ws", "--runtime", "docker-sandbox", "--image", "node:20"], "--image"),
    ],
)
def test_a_flag_for_another_runtime_is_refused(capsys, argv, rejected):
    assert cli.main(argv) == 1
    error = capsys.readouterr().err
    assert "does not apply to --runtime" in error
    assert rejected in error


def test_doctor_reports_the_apple_backend(capsys, fake_container, git_repo):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="status running\n")
    code = cli.main(
        ["doctor", "--runtime", "apple-container", "--path", str(git_repo)]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "runtime apple-container" in out
    assert "all checks pass" in out


def test_doctor_fails_when_the_system_service_is_down(capsys, fake_container, git_repo):
    fake_container.respond("--version", stdout="container CLI version 1.2.2\n")
    fake_container.respond("system", "status", stdout="not running\n", returncode=1)
    code = cli.main(
        ["doctor", "--runtime", "apple-container", "--path", str(git_repo)]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "container system start" in out
