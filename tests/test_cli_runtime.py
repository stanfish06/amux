"""Task 5.5: the CLI surface for the optional sandbox backend.

Three properties this file exists to hold: the help text says the backend is
optional, no command installs `sbx` / signs anyone in / widens Docker policy,
and a sandbox-only flag under the default runtime is refused rather than
quietly ignored.

`no_real_sbx` (conftest) raises if anything here reaches a real `sbx` or
`docker`, so every check below runs against the fake.
"""

from __future__ import annotations

import pytest

from amux import cli, context_service, runtime, sandbox


# --- the backend is optional, and says so ---


def _help(capsys, *argv: str) -> str:
    """Help text with its line wrapping removed.

    argparse breaks phrases like "docker-sandbox only" across lines at whatever
    the terminal width is, so nothing can be matched against the raw output.
    """
    with pytest.raises(SystemExit):
        cli.main([*argv, "--help"])
    return " ".join(capsys.readouterr().out.split()).replace("- ", "-")


def _option_help(capsys, command: str, flag: str) -> str:
    """One option's description, not the usage line that also mentions it."""
    text = _help(capsys, command)
    _, _, options = text.partition("options:")
    assert options, "no options section in the help text"
    return options.split(flag, 1)[1]


@pytest.mark.parametrize("command", ["spw", "spg", "doctor"])
def test_help_calls_the_backend_optional(capsys, command):
    text = _help(capsys, command)
    assert "--runtime" in text
    assert "optional" in text
    assert "docker-sandbox" in text


def test_host_is_the_default_runtime_for_spawning(capsys):
    for command in ("spw", "spg"):
        assert "default: host" in _help(capsys, command)


def test_doctor_defaults_to_the_backend_it_exists_to_check(capsys):
    text = _help(capsys, "doctor")
    assert "default: docker-sandbox" in text
    assert "read-only" in _help(capsys)


def test_every_sandbox_flag_says_it_is_sandbox_only(capsys):
    for flag in ("--cpus", "--memory", "--share-skills", "--context-port"):
        assert "docker-sandbox only" in _option_help(capsys, "spw", flag)[:220], flag


def test_the_resource_defaults_in_help_are_the_real_ones(capsys):
    text = _help(capsys, "spw")
    defaults = sandbox.Resources()
    assert f"default: {defaults.cpus}" in text
    assert f"default: {defaults.memory}" in text
    assert str(context_service.DEFAULT_PORT) in text


def test_shared_skills_is_opt_in_and_explains_why(capsys):
    body = _option_help(capsys, "spw", "--share-skills")[:250]
    assert "read-write" in body
    assert "default: off" in body


# --- sandbox-only flags are refused, not ignored ---


@pytest.mark.parametrize(
    "argv",
    [
        ["spw", "ws", "--cpus", "4"],
        ["spw", "ws", "--memory", "8g"],
        ["spw", "ws", "--share-skills"],
        ["spw", "ws", "--context-port", "5000"],
        ["spg", "ws", "task", "--cpus", "4"],
    ],
)
def test_a_sandbox_flag_under_the_host_runtime_is_refused(capsys, argv):
    assert cli.main(argv) == 1
    error = capsys.readouterr().err
    assert "only applies to --runtime docker-sandbox" in error
    assert argv[-2] in error or argv[-1] in error


def test_the_refusal_names_every_offending_flag(capsys):
    assert cli.main(["spw", "ws", "--cpus", "4", "--memory", "8g"]) == 1
    error = capsys.readouterr().err
    assert "--cpus" in error and "--memory" in error


def test_host_spawning_still_takes_no_runtime_argument():
    """`_resolve_runtime` returns None for the host runtime, which is what keeps
    the default path on `core`'s own default rather than a wrapper."""
    args = _spawn_args(runtime="host")
    assert cli._resolve_runtime(args) is None


def _spawn_args(**overrides):
    class Args:
        runtime = "host"
        cpus = None
        memory = None
        share_skills = False
        context_port = None

    args = Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


# --- resource validation happens before anything is touched ---


@pytest.mark.parametrize(
    ("field", "value"),
    [("cpus", 0), ("cpus", -1), ("memory", "lots"), ("memory", "4"), ("memory", "")],
)
def test_a_bad_resource_value_is_refused_before_any_mutation(capsys, field, value):
    argv = ["spw", "ws", "--runtime", "docker-sandbox", f"--{field}", str(value)]
    assert cli.main(argv) == 1
    error = capsys.readouterr().err
    assert error.startswith("amux: ")
    assert "Traceback" not in error


def test_zero_cpus_is_refused_because_it_is_not_a_cap(capsys):
    assert cli.main(["spw", "ws", "--runtime", "docker-sandbox", "--cpus", "0"]) == 1
    assert "not a cap" in capsys.readouterr().err


def test_the_sandbox_runtime_is_built_with_the_flags_as_given():
    args = _spawn_args(
        runtime="docker-sandbox", cpus=6, memory="12g", share_skills=True, context_port=5001
    )
    chosen = cli._resolve_runtime(args)
    assert isinstance(chosen, runtime.SandboxRuntime)
    assert chosen.config.resources == sandbox.Resources(
        cpus=6, memory="12g", share_skills=True
    )
    assert chosen.config.resolved_port == 5001
    assert chosen.config.policy_target == "localhost:5001"
    assert chosen.config.client_endpoint == "http://host.docker.internal:5001"


def test_unset_flags_fall_back_to_the_documented_defaults():
    chosen = cli._resolve_runtime(_spawn_args(runtime="docker-sandbox"))
    assert chosen.config.resources == sandbox.Resources()
    assert chosen.config.resolved_port == context_service.DEFAULT_PORT
    assert chosen.config.resources.share_skills is False


def test_shared_skills_off_means_the_flag_is_passed_to_sbx():
    chosen = cli._resolve_runtime(_spawn_args(runtime="docker-sandbox"))
    assert "--no-share-skills" in chosen.config.resources.create_flags()
    opted_in = cli._resolve_runtime(
        _spawn_args(runtime="docker-sandbox", share_skills=True)
    )
    assert "--no-share-skills" not in opted_in.config.resources.create_flags()


# --- doctor ---


def test_doctor_reports_every_check_and_fails_closed(capsys, fake_sbx, git_repo):
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json("diagnose", payload={"checks": []})
    fake_sbx.respond("policy", stdout="", returncode=1)

    code = cli.main(["doctor", "--path", str(git_repo)])
    out = capsys.readouterr().out
    assert code == 1
    assert "docker-sandbox (optional backend)" in out
    for check in ("resources", "agents", "repository", "sbx", "network-policy"):
        assert check in out
    assert "check(s) failed" in out
    assert "amux changes nothing on its own" in out


def test_doctor_is_read_only(capsys, fake_sbx, git_repo):
    """Not an assertion about intent: these are the only `sbx` subcommands it
    may reach, and none of them creates, installs, signs in, or writes policy.
    """
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json("diagnose", payload={"checks": []})
    cli.main(["doctor", "--path", str(git_repo)])

    reached = {call[0] for call in fake_sbx.calls if call}
    assert reached <= {"version", "--version", "ls", "diagnose", "policy"}
    for call in fake_sbx.calls:
        argv = " ".join(call)
        assert "create" not in argv
        assert "login" not in argv
        assert "policy init" not in argv
        assert "policy allow" not in argv


def test_doctor_prints_the_policy_command_without_running_it(capsys, fake_sbx, git_repo):
    """The gap misty-panda found: `sbx diagnose` passes every check, including
    Authentication, while network policy is uninitialised. Authenticated does
    not imply reachable, so policy gets its own probe and its own remediation.
    """
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json(
        "diagnose",
        payload={
            "checks": [
                {"name": "Authentication", "status": "pass", "message": "signed in"}
            ]
        },
    )
    fake_sbx.respond("policy", stdout="policy is not initialized", returncode=1)

    code = cli.main(["doctor", "--path", str(git_repo)])
    out = capsys.readouterr().out
    assert code == 1
    assert "network-policy" in out
    assert "sbx policy" in out  # the command to run, printed not run
    assert not any(
        "allow" in " ".join(call) or "init" in " ".join(call)
        for call in fake_sbx.calls
    )


def test_doctor_for_the_host_runtime_needs_nothing_external(capsys):
    assert cli.main(["doctor", "--runtime", "host"]) == 0
    out = capsys.readouterr().out
    assert "no external prerequisites" in out


def test_doctor_says_so_when_sbx_is_absent(capsys, tmp_path, no_sbx):
    """A machine that has not installed the optional backend.

    `no_sbx` gives PATH a single empty directory, so git is missing too and the
    report says both; the claim under test is the `sbx` line and its
    remediation.
    """
    code = cli.main(["doctor", "--path", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] sbx" in out
    assert "install Docker Sandboxes" in out
    # ...and it does not offer to do it.
    assert "installing" not in out.lower()


def test_doctor_reports_a_missing_git_instead_of_aborting(capsys, monkeypatch, fake_sbx, tmp_path):
    """A doctor reports what it found; aborting on the first failure teaches the
    user one prerequisite per run. PATH keeps the fake `sbx` and loses git."""
    monkeypatch.setenv("PATH", str(fake_sbx.bin_dir))
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json("diagnose", payload={"checks": []})

    code = cli.main(["doctor", "--path", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "cannot run git" in out
    assert "install git" in out
    # The rest of the report still ran.
    assert "[ok] sbx" in out
    assert "check(s) failed" in out


def test_doctor_rejects_a_secondary_worktree_with_the_reason(capsys, fake_sbx, git_repo, git_run):
    """The trap worth naming: amux's own agents live in secondary worktrees, so
    the obvious `--path` is exactly the one `sbx create --clone` cannot use."""
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json("diagnose", payload={"checks": []})
    secondary = git_repo.parent / "secondary"
    git_run(str(git_repo), "worktree", "add", str(secondary), "-b", "side")

    code = cli.main(["doctor", "--path", str(secondary)])
    out = capsys.readouterr().out
    assert code == 1
    assert "not a primary checkout" in out


# --- context-service ---


def test_context_service_status_when_nothing_runs(capsys):
    assert cli.main(["context-service", "status"]) == 0
    assert "not running" in capsys.readouterr().out


def test_context_service_start_status_stop(capsys, isolate_state):
    assert cli.main(["context-service", "start"]) == 0
    started = capsys.readouterr().out
    assert "running" in started

    assert cli.main(["context-service", "status"]) == 0
    running = capsys.readouterr().out
    assert "running" in running
    port = context_service.read_runfile(context_service.ServiceConfig()).port
    assert str(port) in running
    assert context_service.probe_health(port)["ok"] is True

    assert cli.main(["context-service", "stop"]) == 0
    assert "stopped" in capsys.readouterr().out
    assert context_service.read_runfile(context_service.ServiceConfig()) is None


def test_context_service_start_is_idempotent(capsys, isolate_state):
    try:
        assert cli.main(["context-service", "start"]) == 0
        first = context_service.read_runfile(context_service.ServiceConfig())
        assert cli.main(["context-service", "start"]) == 0
        assert context_service.read_runfile(context_service.ServiceConfig()) == first
    finally:
        cli.main(["context-service", "stop"])


def test_context_service_honours_the_port_flag(capsys, isolate_state):
    port = _bindable_port()
    try:
        assert cli.main(["context-service", "start", "--port", str(port)]) == 0
        assert context_service.read_runfile(context_service.ServiceConfig()).port == port
        assert str(port) in capsys.readouterr().out
    finally:
        cli.main(["context-service", "stop"])


def test_a_bad_port_is_an_error_not_a_traceback(capsys):
    assert cli.main(["context-service", "status", "--port", "99999"]) == 1
    error = capsys.readouterr().err
    assert "65535" in error
    assert "Traceback" not in error


def test_the_cli_and_the_module_report_the_same_state(capsys, isolate_state):
    """Both go through `run_action`, so they cannot drift."""
    assert cli.main(["context-service", "status"]) == 0
    through_cli = capsys.readouterr().out
    assert context_service.main(["status"]) == 0
    through_module = capsys.readouterr().out
    assert through_cli == through_module


# Below the ephemeral range, so no `--port 0` elsewhere in the suite can draw
# it: reading a port from a socket and closing it is a cross-test TOCTOU (note
# #50).
BINDABLE_PORT = 47402


def _bindable_port() -> int:
    assert context_service.probe_health(BINDABLE_PORT, timeout=0.5) is None, (
        f"something is already serving on {BINDABLE_PORT}"
    )
    return BINDABLE_PORT


# --- nothing here changes the host on its own ---


def test_no_command_installs_signs_in_or_widens_policy(capsys, fake_sbx, git_repo):
    """A sweep over every sandbox-aware command that can run without Docker."""
    fake_sbx.respond("version", stdout="sbx version 0.37.1")
    fake_sbx.respond_json("diagnose", payload={"checks": []})
    for argv in (
        ["doctor", "--path", str(git_repo)],
        ["doctor", "--runtime", "host"],
        ["context-service", "status"],
    ):
        cli.main(argv)
        capsys.readouterr()
    forbidden = ("policy init", "policy allow", "login", "sbx create", "brew install")
    argvs = [" ".join(call) for call in fake_sbx.calls]
    for argv in argvs:
        assert not any(bad in argv for bad in forbidden), argv


def test_the_launcher_reexecs_amux_rather_than_a_shell():
    argv = context_service.launch_argv(context_service.ServiceConfig(port=1234))
    assert "serve" in argv
    assert "1234" in argv
    assert not any(part in ("sh", "-c", "bash") for part in argv)


def test_the_frozen_launcher_uses_the_amux_subcommand(monkeypatch):
    """A PyInstaller build has no `-m`: `sys.executable` is the amux binary, so
    it has to be invoked through the subcommand this task adds."""
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    argv = context_service.launch_argv(context_service.ServiceConfig(port=1234))
    assert "-m" not in argv
    assert argv[1:] == ["context-service", "serve", "--port", "1234"]
    assert "context-service" in [a for a in argv]


def test_the_source_launcher_uses_the_module_form(monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    argv = context_service.launch_argv(context_service.ServiceConfig(port=1234))
    assert argv[1] == "-m"
    assert argv[2] == "amux.context_service"
