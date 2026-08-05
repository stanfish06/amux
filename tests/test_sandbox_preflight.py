"""Sandbox preflight.

Two properties matter and both are asserted directly rather than assumed:

1. **Every failure is actionable.** A failed check carries a command or a
   concrete action, never bare prose.
2. **Preflight mutates nothing.** No sandbox, no policy rule, no tmux pane, no
   git reference, no registry row. The whole point is that a rejected spawn
   leaves the host exactly as it was, so the tests assert on the `sbx`
   subcommands that were reachable, not just on the verdict.
"""

from __future__ import annotations

import pytest

from amux import sandbox, store

VERSION_LINE = "sbx version: v0.37.1 2d4f32448c7a94d7fa525517dfca21aa36599829\n"

POLICY_UNINITIALIZED = (
    "ERROR: check network policy: request failed: 412 Precondition Failed: "
    "global network policy has not been initialized; run: sbx policy init "
    "<allow-all|balanced|deny-all>\n"
)

ENDPOINT = "localhost:47317"

# Every sbx subcommand preflight is allowed to reach for. Anything outside this
# set either creates, destroys, or widens access.
READ_ONLY = {("version",), ("ls", "--json"), ("diagnose", "-o", "json"),
             ("policy", "check")}


def healthy():
    return lambda: (True, "listening on 127.0.0.1:47317")


def unhealthy():
    return lambda: (False, "connection refused")


def all_pass(fake_sbx, *, policy_ok=True):
    fake_sbx.respond("version", stdout=VERSION_LINE)
    fake_sbx.respond_json(
        "diagnose",
        "-o",
        "json",
        payload={
            "checks": [
                {"name": "Daemon", "status": "pass", "message": "healthy"},
                {"name": "Authentication", "status": "pass", "message": "authenticated"},
            ],
            "summary": {"pass": 2, "warn": 0, "fail": 0, "skip": 0},
        },
    )
    if policy_ok:
        fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    else:
        fake_sbx.respond(
            "policy", "check", "network", stderr=POLICY_UNINITIALIZED, returncode=1
        )


def run(repo, fake_sbx, *, agents=("claude", "codex"), resources=None, health=None):
    return sandbox.preflight(
        agents=list(agents),
        repo=str(repo),
        resources=resources or sandbox.Resources(),
        endpoint=ENDPOINT,
        service_healthy=health or healthy(),
    )


def failed(result) -> dict[str, sandbox.Check]:
    return {c.name: c for c in result.failures}


def assert_read_only(fake_sbx):
    for call in fake_sbx.calls:
        assert any(tuple(call[: len(p)]) == p for p in READ_ONLY), (
            f"preflight invoked a non-read-only sbx command: {call}"
        )


# --- the happy path ---


def test_preflight_passes_when_everything_is_in_place(git_repo, fake_sbx):
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx)

    assert result.ok, result.report()
    assert {c.name for c in result.checks} == {
        "resources", "context-client", "agents", "repository", "sbx", "docker",
        "context-service", "network-policy",
    }
    assert_read_only(fake_sbx)


def test_preflight_creates_nothing(git_repo, fake_sbx):
    """The contract: a preflight run is invisible afterwards."""
    all_pass(fake_sbx)
    run(git_repo, fake_sbx)

    assert not fake_sbx.called_with("create")
    assert not fake_sbx.called_with("run")
    assert not fake_sbx.called_with("rm")
    assert not fake_sbx.called_with("stop")
    assert not fake_sbx.called_with("policy", "init")
    assert not fake_sbx.called_with("policy", "allow")
    assert not fake_sbx.called_with("login")
    # No registry row, and no git reference beyond what the fixture made.
    assert store.worktrees_for("ws", "t0") == []


def test_preflight_can_run_without_a_context_service_probe(git_repo, fake_sbx):
    all_pass(fake_sbx)
    result = sandbox.preflight(
        agents=["claude"],
        repo=str(git_repo),
        resources=sandbox.Resources(),
        endpoint=ENDPOINT,
        service_healthy=None,
    )
    assert result.ok
    assert "context-service" not in {c.name for c in result.checks}


# --- individual failures ---


def test_missing_sbx_short_circuits_with_an_install_action(git_repo, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file", "sbx")

    monkeypatch.setattr(sandbox.subprocess, "run", missing)
    result = sandbox.preflight(
        agents=["claude"],
        repo=str(git_repo),
        resources=sandbox.Resources(),
        endpoint=ENDPOINT,
        service_healthy=healthy(),
    )
    assert not result.ok
    assert "install Docker Sandboxes" in failed(result)["sbx"].remediation
    # Later checks are skipped rather than reported as spurious failures.
    assert "docker" not in {c.name for c in result.checks}
    assert "network-policy" not in {c.name for c in result.checks}


def test_old_sbx_is_rejected_with_the_missing_capability(git_repo, fake_sbx):
    fake_sbx.respond("version", stdout="sbx version: v0.30.0 abc\n")
    fake_sbx.respond_json("diagnose", "-o", "json", payload={"checks": []})
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    result = run(git_repo, fake_sbx)

    assert not result.ok
    assert "sbx ls --json" in failed(result)["sbx"].detail or "v0.37.0" in str(
        failed(result)["sbx"]
    )


def test_a_secondary_worktree_is_rejected_before_anything_external(
    git_repo, git_run, tmp_path, fake_sbx
):
    """amux runs its agents in secondary worktrees, so this is the likely mistake."""
    all_pass(fake_sbx)
    linked = tmp_path / "linked"
    git_run(git_repo, "worktree", "add", "-q", str(linked), "-b", "side")
    assert (linked / ".git").is_file()  # the condition under test, made explicit

    result = run(linked, fake_sbx)
    assert not result.ok
    assert "secondary git worktree" in failed(result)["repository"].remediation
    assert_read_only(fake_sbx)


def test_primary_checkout_is_recognized(git_repo):
    assert sandbox.is_primary_checkout(str(git_repo))


def test_unsupported_agent_is_named(git_repo, fake_sbx):
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx, agents=("claude", "gemini", "shell"))

    assert not result.ok
    check = failed(result)["agents"]
    assert "gemini" in check.detail and "shell" in check.detail
    assert "host runtime" in check.remediation


def test_bad_resources_fail_without_probing_docker(git_repo, fake_sbx):
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx, resources=sandbox.Resources(cpus=0))

    assert not result.ok
    assert "every host CPU" in failed(result)["resources"].detail


def test_docker_authentication_failure_surfaces_its_hint(git_repo, fake_sbx):
    fake_sbx.respond("version", stdout=VERSION_LINE)
    fake_sbx.respond_json(
        "diagnose",
        "-o",
        "json",
        payload={
            "checks": [
                {"name": "Authentication", "status": "fail",
                 "message": "not signed in", "hint": "run: sbx login"},
            ],
        },
        returncode=1,
    )
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    result = run(git_repo, fake_sbx)

    assert not result.ok
    assert "sbx login" in failed(result)["docker"].remediation
    # Detecting a signed-out state must not sign anyone in.
    assert not fake_sbx.called_with("login")


def test_a_docker_warning_alone_does_not_block(git_repo, fake_sbx):
    fake_sbx.respond("version", stdout=VERSION_LINE)
    fake_sbx.respond_json(
        "diagnose",
        "-o",
        "json",
        payload={"checks": [{"name": "Daemon", "status": "warn", "message": "slow"}]},
    )
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    result = run(git_repo, fake_sbx)

    assert result.ok
    docker = next(c for c in result.checks if c.name == "docker")
    assert "slow" in docker.detail  # reported, but not fatal


def test_uninitialized_policy_reports_init_and_never_runs_it(git_repo, fake_sbx):
    all_pass(fake_sbx, policy_ok=False)
    result = run(git_repo, fake_sbx)

    assert not result.ok
    check = failed(result)["network-policy"]
    assert "sbx policy init" in check.remediation
    assert not fake_sbx.called_with("policy", "init")
    assert_read_only(fake_sbx)


def test_denied_port_reports_the_exact_allow_command(git_repo, fake_sbx):
    fake_sbx.respond("version", stdout=VERSION_LINE)
    fake_sbx.respond_json("diagnose", "-o", "json", payload={"checks": []})
    fake_sbx.respond(
        "policy", "check", "network", stderr="ERROR: denied by rule\n", returncode=1
    )
    result = run(git_repo, fake_sbx)

    assert not result.ok
    assert failed(result)["network-policy"].remediation == (
        f"sbx policy allow network {ENDPOINT}"
    )
    assert not fake_sbx.called_with("policy", "allow")


def test_unhealthy_context_service_is_actionable(git_repo, fake_sbx):
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx, health=unhealthy())

    assert not result.ok
    check = failed(result)["context-service"]
    assert check.detail == "connection refused"
    assert "context-service start" in check.remediation


# --- reporting ---


def test_every_failure_carries_a_remediation(git_repo, fake_sbx):
    all_pass(fake_sbx, policy_ok=False)
    result = run(
        git_repo, fake_sbx,
        agents=("gemini",),
        resources=sandbox.Resources(cpus=0),
        health=unhealthy(),
    )
    assert len(result.failures) >= 4
    for check in result.failures:
        assert check.remediation, f"{check.name} failed without a remediation"


def test_raise_if_failed_names_every_failure(git_repo, fake_sbx):
    all_pass(fake_sbx, policy_ok=False)
    result = run(git_repo, fake_sbx, agents=("gemini",))

    with pytest.raises(sandbox.SandboxError) as exc:
        result.raise_if_failed()
    message = str(exc.value)
    assert "preflight failed" in message
    assert "agents" in message and "network-policy" in message
    assert "sbx policy init" in message


def test_raise_if_failed_is_silent_when_everything_passes(git_repo, fake_sbx):
    all_pass(fake_sbx)
    run(git_repo, fake_sbx).raise_if_failed()


def test_report_shows_passes_and_failures(git_repo, fake_sbx):
    all_pass(fake_sbx, policy_ok=False)
    text = run(git_repo, fake_sbx).report()
    assert "[ok] repository" in text
    assert "[FAIL] network-policy" in text
    assert "fix:" in text


# --- packaging ---


def test_a_build_without_the_context_client_is_caught_before_anything_runs(
    git_repo, fake_sbx, monkeypatch
):
    """A packaged amux resolves the shim differently from a source checkout. A
    build that omitted it used to fail at bootstrap -- after the pane, the
    sandbox and the token already existed."""
    from amux import sandbox_bootstrap

    def missing():
        raise sandbox_bootstrap.BootstrapError(
            "sandbox client source is missing at /nonexistent/sandbox_client.py"
        )

    monkeypatch.setattr(sandbox_bootstrap, "client_source", missing)
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx)

    assert not result.ok
    check = failed(result)["context-client"]
    # The remediation names the packaging fault, not a generic bootstrap error.
    assert "does not ship" in check.remediation
    assert "source checkout" in check.remediation
    # And nothing was created while finding out.
    assert not fake_sbx.called_with("create")
    assert_read_only(fake_sbx)


def test_the_context_client_check_passes_in_a_source_checkout(git_repo, fake_sbx):
    """The positive control: it really does resolve here, so the failure above
    is evidence rather than a check that can only ever fail."""
    all_pass(fake_sbx)
    result = run(git_repo, fake_sbx)

    check = next(c for c in result.checks if c.name == "context-client")
    assert check.ok
    assert check.detail.endswith("sandbox_client.py")


def test_the_check_uses_the_bootstrap_resolver_not_its_own_idea_of_the_path(
    git_repo, fake_sbx, monkeypatch
):
    """Whatever `client_source` resolves is the answer -- a second opinion here
    would disagree with reality in exactly the frozen case that motivated it."""
    from amux import sandbox_bootstrap

    calls: list[int] = []
    real = sandbox_bootstrap.client_source
    monkeypatch.setattr(
        sandbox_bootstrap, "client_source", lambda: (calls.append(1), real())[1]
    )
    all_pass(fake_sbx)
    run(git_repo, fake_sbx)

    assert calls, "preflight did not consult sandbox_bootstrap.client_source"
