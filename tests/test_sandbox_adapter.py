"""The `sbx` adapter's command surface and parsing.

`sbx` is an external tool on its own release cadence, so the recorded argv
lists in these tests *are* the contract. Every command and every parsed shape
here was verified against the real sbx v0.37.1 before being written down; the
fake replays that surface so the suite stays offline.

Where design.md and the real CLI disagreed, the CLI won — notably `sbx ls
--json` in place of a nonexistent `sbx inspect`, and `sbx version` in place of
a nonexistent `--version` flag.
"""

from __future__ import annotations

import subprocess

import pytest

from amux import sandbox

VERSION_LINE = "sbx version: v0.37.1 2d4f32448c7a94d7fa525517dfca21aa36599829\n"

# The real 412 sbx emits on stderr while the global policy is uninitialized.
POLICY_UNINITIALIZED = (
    "ERROR: check network policy: request failed: 412 Precondition Failed: "
    "global network policy has not been initialized; run: sbx policy init "
    "<allow-all|balanced|deny-all>\n"
)


# --- version ---


def test_version_parses_the_real_output_format(fake_sbx):
    fake_sbx.respond("version", stdout=VERSION_LINE)
    assert sandbox.version() == "v0.37.1"
    assert fake_sbx.calls == [["version"]]


def test_version_rejects_unreadable_output(fake_sbx):
    fake_sbx.respond("version", stdout="sbx: something else entirely\n")
    with pytest.raises(sandbox.SandboxError, match="could not read an sbx version"):
        sandbox.version()


@pytest.mark.parametrize(
    "text,supported",
    [
        ("v0.37.1", True),
        ("v0.37.0", True),
        ("v1.2.0", True),
        ("v0.36.9", False),
        ("v0.9.0", False),
    ],
)
def test_supported_versions(text, supported):
    assert sandbox.is_supported(text) is supported
    assert bool(sandbox.unsupported_reason(text)) is not supported


def test_unsupported_reason_names_the_missing_capability():
    reason = sandbox.unsupported_reason("v0.30.0")
    assert "v0.37.0" in reason and "sbx ls --json" in reason


# --- naming ---


def test_sandbox_name_is_deterministic_and_scoped(tmp_path):
    repo = str(tmp_path / "proj")
    first = sandbox.sandbox_name("amux", "docker", "misty-panda", repo)
    assert first == sandbox.sandbox_name("amux", "docker", "misty-panda", repo)
    assert first.startswith("amux-amux-docker-misty-panda-")


def test_sandbox_name_separates_identically_named_repos(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = sandbox.sandbox_name("ws", "t0", "alpha", str(tmp_path / "a"))
    b = sandbox.sandbox_name("ws", "t0", "alpha", str(tmp_path / "b"))
    assert a != b
    # Only the fingerprint differs; the readable part still identifies the agent.
    assert a.rsplit("-", 1)[0] == b.rsplit("-", 1)[0]


def test_sandbox_name_uses_only_characters_sbx_accepts():
    name = sandbox.sandbox_name("My Work/Space", "task #1", "cle_ver mole", "/tmp/r")
    assert name == "".join(c for c in name if c.isalnum() or c in ".+-")
    assert " " not in name and "/" not in name and "_" not in name


def test_sandbox_name_survives_fully_unusable_components():
    name = sandbox.sandbox_name("///", "___", "!!!", "/tmp/r")
    assert name.startswith("amux-x-x-x-")


def test_repo_fingerprint_follows_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert sandbox.repo_fingerprint(str(link)) == sandbox.repo_fingerprint(str(real))


def test_git_remote_matches_dockers_naming():
    assert sandbox.git_remote("amux-ws-t0-alpha-deadbeef") == (
        "sandbox-amux-ws-t0-alpha-deadbeef"
    )


# --- resources ---


def test_default_resources_are_explicit_caps():
    res = sandbox.Resources()
    res.validate()
    assert res.cpus >= 1 and res.memory
    assert res.share_skills is False


def test_resource_flags_disable_shared_skills_by_default():
    flags = sandbox.Resources(cpus=2, memory="4g").create_flags()
    assert flags == ("--cpus", "2", "--memory", "4g", "--no-share-skills")


def test_shared_skills_are_opt_in():
    """The absence below is only evidence if the flags were really produced --
    an empty tuple would satisfy `not in` while proving nothing."""
    flags = sandbox.Resources(cpus=1, memory="1g", share_skills=True).create_flags()
    assert flags == ("--cpus", "1", "--memory", "1g")
    assert "--no-share-skills" not in flags


def test_shared_skills_opt_in_is_the_only_difference():
    """Pin the pair: the opt-in changes exactly one thing and nothing else."""
    off = sandbox.Resources(cpus=1, memory="1g").create_flags()
    on = sandbox.Resources(cpus=1, memory="1g", share_skills=True).create_flags()
    assert set(off) - set(on) == {"--no-share-skills"}
    assert set(on) - set(off) == set()


@pytest.mark.parametrize("memory", ["4g", "1024m", "512M", "8gb", "2gi", "16G"])
def test_accepted_memory_sizes(memory):
    sandbox.Resources(memory=memory).validate()


@pytest.mark.parametrize("memory", ["", "lots", "4", "-4g", "g", "4 gigs"])
def test_rejected_memory_sizes(memory):
    with pytest.raises(sandbox.SandboxError, match="binary units"):
        sandbox.Resources(memory=memory).validate()


def test_zero_cpus_is_rejected_because_it_is_not_a_cap():
    with pytest.raises(sandbox.SandboxError, match="every host CPU"):
        sandbox.Resources(cpus=0).validate()


@pytest.mark.parametrize("cpus", [-1, 1000])
def test_implausible_cpu_counts_are_rejected(cpus):
    with pytest.raises(sandbox.SandboxError):
        sandbox.Resources(cpus=cpus).validate()


def test_non_integer_cpus_are_rejected():
    with pytest.raises(sandbox.SandboxError, match="whole number"):
        sandbox.Resources(cpus="2").validate()  # type: ignore[arg-type]


# --- create ---


def test_create_argv_pins_the_clone_mode_command():
    argv = sandbox.create_argv(
        "amux-ws-t0-alpha-deadbeef",
        "claude",
        "/repo",
        sandbox.Resources(cpus=2, memory="4g"),
    )
    assert argv == (
        "create",
        "--clone",
        "--name",
        "amux-ws-t0-alpha-deadbeef",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--no-share-skills",
        "claude",
        "/repo",
    )


def test_create_argv_rejects_unsupported_agents():
    with pytest.raises(sandbox.SandboxError, match="not supported"):
        sandbox.create_argv("n", "gemini", "/repo", sandbox.Resources())


def test_unsupported_agent_is_rejected_before_any_sbx_call(fake_sbx):
    with pytest.raises(sandbox.SandboxError):
        sandbox.create("n", "shell", "/repo", sandbox.Resources())
    assert fake_sbx.calls == []


def test_invalid_resources_are_rejected_before_any_sbx_call(fake_sbx):
    with pytest.raises(sandbox.SandboxError):
        sandbox.create("n", "claude", "/repo", sandbox.Resources(cpus=0))
    assert fake_sbx.calls == []


def test_create_binds_the_handle_to_the_reported_identity(fake_sbx):
    fake_sbx.respond("create")
    fake_sbx.respond_json(
        "ls", "--json", payload={"sandboxes": [{"name": "sb1", "id": "sbx_abc123"}]}
    )
    handle = sandbox.create("sb1", "codex", "/repo", sandbox.Resources())

    assert handle.name == "sb1"
    assert handle.id == "sbx_abc123"
    assert handle.git_remote == "sandbox-sb1"
    assert fake_sbx.called_with("create", "--clone", "--name", "sb1")


def test_create_refuses_to_invent_an_identity(fake_sbx):
    """`sbx` said yes but the sandbox is not listed: never guess the id."""
    fake_sbx.respond("create")
    fake_sbx.respond_json("ls", "--json", payload={"sandboxes": []})
    with pytest.raises(sandbox.SandboxError, match="refusing to guess"):
        sandbox.create("sb1", "claude", "/repo", sandbox.Resources())


# --- inspection ---


def test_sandboxes_parses_the_ls_envelope(fake_sbx):
    fake_sbx.respond_json(
        "ls",
        "--json",
        payload={"sandboxes": [{"name": "a"}, {"name": "b"}]},
    )
    assert [s["name"] for s in sandbox.sandboxes()] == ["a", "b"]
    assert fake_sbx.calls == [["ls", "--json"]]


def test_sandboxes_is_empty_when_none_exist(fake_sbx):
    fake_sbx.respond_json("ls", "--json", payload={"sandboxes": []})
    assert sandbox.sandboxes() == []


@pytest.mark.parametrize(
    "payload", [{"items": []}, [], {"sandboxes": "none"}, {"sandboxes": None}]
)
def test_sandboxes_rejects_an_unexpected_shape(fake_sbx, payload):
    fake_sbx.respond_json("ls", "--json", payload=payload)
    with pytest.raises(sandbox.SandboxError, match="unexpected"):
        sandbox.sandboxes()


def test_sandboxes_rejects_unparseable_output(fake_sbx):
    fake_sbx.respond("ls", "--json", stdout="not json at all")
    with pytest.raises(sandbox.SandboxError, match="could not parse"):
        sandbox.sandboxes()


def test_find_and_exists(fake_sbx):
    fake_sbx.respond_json(
        "ls", "--json", payload={"sandboxes": [{"name": "a", "id": "1"}]}
    )
    assert sandbox.find("a") == {"name": "a", "id": "1"}
    assert sandbox.find("missing") is None
    assert sandbox.exists("a") and not sandbox.exists("missing")


def test_diagnose_parses_checks_and_survives_failure_exit(fake_sbx):
    fake_sbx.respond_json(
        "diagnose",
        "-o",
        "json",
        payload={
            "version": "1.0",
            "checks": [
                {"name": "CLI binary", "status": "pass", "message": "found"},
                {"name": "Authentication", "status": "fail", "message": "signed out",
                 "hint": "run sbx login"},
                {"name": "Daemon", "status": "warn", "message": "slow"},
            ],
            "summary": {"pass": 1, "warn": 1, "fail": 1, "skip": 0},
        },
        returncode=1,
    )
    report = sandbox.diagnose()
    assert report["summary"]["fail"] == 1
    # Worst first, so a caller reporting one line reports the real blocker.
    assert [c["status"] for c in sandbox.failed_checks(report)] == ["fail", "warn"]
    assert sandbox.failed_checks(report)[0]["name"] == "Authentication"


def test_diagnose_reports_nothing_when_everything_passes(fake_sbx):
    fake_sbx.respond_json(
        "diagnose",
        "-o",
        "json",
        payload={"checks": [{"name": "Daemon", "status": "pass", "message": "ok"}]},
    )
    assert sandbox.failed_checks(sandbox.diagnose()) == []


def test_diagnose_rejects_an_unexpected_shape(fake_sbx):
    fake_sbx.respond_json("diagnose", "-o", "json", payload={"checks": "fine"})
    with pytest.raises(sandbox.SandboxError, match="unexpected"):
        sandbox.diagnose()


# --- network policy ---


def test_policy_check_is_read_only_and_reports_allowed(fake_sbx):
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    check = sandbox.check_network("localhost:47317")
    assert check.allowed and check.initialized
    assert fake_sbx.calls == [["policy", "check", "network", "localhost:47317"]]
    # Nothing that could widen policy was run.
    assert not fake_sbx.called_with("policy", "allow")
    assert not fake_sbx.called_with("policy", "init")


def test_policy_check_detects_an_uninitialized_global_policy(fake_sbx):
    fake_sbx.respond(
        "policy", "check", "network", stderr=POLICY_UNINITIALIZED, returncode=1
    )
    check = sandbox.check_network("localhost:47317")
    assert not check.allowed
    assert not check.initialized
    assert "sbx policy init" in check.remediation
    # amux reports the command; it must never run it.
    assert not fake_sbx.called_with("policy", "init")


def test_policy_check_distinguishes_denied_from_uninitialized(fake_sbx):
    fake_sbx.respond(
        "policy", "check", "network", stderr="ERROR: denied by rule\n", returncode=1
    )
    check = sandbox.check_network("localhost:47317")
    assert not check.allowed
    assert check.initialized


def test_policy_check_can_scope_to_one_sandbox(fake_sbx):
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    sandbox.check_network("localhost:47317", sandbox="sb1")
    assert fake_sbx.calls == [
        ["policy", "check", "network", "localhost:47317", "--sandbox", "sb1"]
    ]


def test_allow_network_command_is_text_not_an_invocation(fake_sbx):
    assert sandbox.allow_network_command("localhost:47317") == (
        "sbx policy allow network localhost:47317"
    )
    assert sandbox.allow_network_command("localhost:47317", sandbox="sb1") == (
        "sbx policy allow network --sandbox sb1 localhost:47317"
    )
    assert fake_sbx.calls == []


# --- lifecycle and ops ---


def test_attach_omits_the_agent_so_a_sandbox_is_reattached_not_recreated():
    assert sandbox.attach_argv("sb1") == ("run", "--name", "sb1")
    assert sandbox.attach_argv("sb1", "claude") == ("run", "--name", "sb1")
    assert sandbox.attach_command("sb1", "claude") == "sbx run --name sb1"


def test_codex_attach_carries_the_hook_trust_flag():
    """Codex silently skips hooks it has no persisted trust for, so without
    this a sandboxed Codex reports no state at all and looks permanently idle.
    Live-verified on codex 0.146.0; invisible to any offline behaviour test,
    which is why the argv itself is pinned."""
    assert sandbox.attach_argv("sb1", "codex") == (
        "run", "--name", "sb1", "codex", "--", "--dangerously-bypass-hook-trust",
    )
    assert sandbox.attach_command("sb1", "codex") == (
        "sbx run --name sb1 codex -- --dangerously-bypass-hook-trust"
    )


def test_claude_attach_does_not_carry_the_hook_trust_flag():
    """The counterpart: it is a Codex-only workaround and must not spread to an
    agent that does not need it."""
    for agent in ("claude", ""):
        assert sandbox.HOOK_TRUST_FLAG not in sandbox.attach_argv("sb1", agent)


def test_stop_and_remove(fake_sbx):
    fake_sbx.respond("stop")
    fake_sbx.respond("rm")
    sandbox.stop("sb1")
    sandbox.remove("sb1")
    sandbox.remove("sb2", force=True)
    assert fake_sbx.calls == [["stop", "sb1"], ["rm", "sb1"], ["rm", "-f", "sb2"]]


def test_copy_in_sends_the_file_without_putting_it_in_argv(fake_sbx, tmp_path):
    fake_sbx.respond("cp")
    secret = tmp_path / "client.json"
    secret.write_text('{"token": "s3cret"}')
    sandbox.Sandbox(name="sb1").copy_in(secret, "/home/agent/.amux/client.json")

    (call,) = fake_sbx.calls
    assert call == ["cp", str(secret), "sb1:/home/agent/.amux/client.json"]
    assert "s3cret" not in " ".join(call)


def test_copy_in_requires_an_absolute_destination(fake_sbx, tmp_path):
    src = tmp_path / "f"
    src.write_text("x")
    with pytest.raises(sandbox.SandboxError, match="absolute"):
        sandbox.Sandbox(name="sb1").copy_in(src, "relative/path")
    assert fake_sbx.calls == []


def test_exec_returns_stdout(fake_sbx):
    fake_sbx.respond("exec", stdout="on-branch\n")
    assert sandbox.Sandbox(name="sb1").exec(["git", "branch", "--show-current"]) == (
        "on-branch\n"
    )
    assert fake_sbx.calls == [["exec", "sb1", "git", "branch", "--show-current"]]


def test_exec_raises_on_failure(fake_sbx):
    fake_sbx.respond("exec", stderr="fatal: not a git repository\n", returncode=128)
    with pytest.raises(sandbox.SandboxError, match="not a git repository"):
        sandbox.Sandbox(name="sb1").exec(["git", "status"])


def test_exec_rejects_an_empty_command(fake_sbx):
    with pytest.raises(sandbox.SandboxError, match="needs a command"):
        sandbox.Sandbox(name="sb1").exec([])
    assert fake_sbx.calls == []


def test_sandbox_satisfies_the_bootstrap_ops_protocol(tmp_path):
    """`sandbox_bootstrap` drives name/copy_in/exec; nothing else is required."""
    handle = sandbox.Sandbox(name="sb1", id="sbx_1")
    assert isinstance(handle.name, str)
    assert callable(handle.copy_in) and callable(handle.exec)


# --- failure surfaces ---


def test_missing_executable_is_reported_as_actionable(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "sbx")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(sandbox.SandboxError, match="not installed"):
        sandbox.version()


def test_timeout_is_reported_with_the_command(monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sbx ls", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(sandbox.SandboxError, match="timed out"):
        sandbox.sandboxes()


def test_failed_commands_surface_the_first_stderr_line(fake_sbx):
    fake_sbx.respond("stop", stderr="ERROR: no such sandbox: sb9\nstack trace\n",
                     returncode=1)
    with pytest.raises(sandbox.SandboxError, match="no such sandbox: sb9"):
        sandbox.stop("sb9")


def test_unchecked_commands_return_their_failure(fake_sbx):
    fake_sbx.respond("policy", "check", "network", stderr="nope\n", returncode=1)
    result = sandbox.run("policy", "check", "network", "x:1", check=False)
    assert not result.ok and result.message == "nope"


# The real output of a *denial* on a host where `sbx policy init` HAS been run.
# Captured from sbx v0.37.1. Note it goes to stdout, not stderr, and looks
# nothing like the uninitialized-policy error.
POLICY_DENIED = (
    "Denied: localhost:47317\n"
    "Governance: Local policy only\n"
    "Context: global\n"
    "Reason: no matching allow rule (default deny)\n"
)


def test_policy_check_handles_a_real_default_deny(fake_sbx):
    """The live negative case: policy initialized, this port not allowed."""
    fake_sbx.respond(
        "policy", "check", "network", stdout=POLICY_DENIED, returncode=1
    )
    check = sandbox.check_network("localhost:47317")

    assert not check.allowed
    # Initialized -- so the remediation is "allow this port", not "run init".
    assert check.initialized
    assert check.detail == "Denied: localhost:47317"
    assert "policy init" not in check.remediation
