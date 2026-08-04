"""The Docker Sandbox runtime: creation, identity, and attachment.

The spec's headline scenario is a mixed Claude/Codex grid producing one capped,
no-shared-skills sandbox per pane, each with its own branch, capability and
registry row. That is asserted end to end here against the fake `sbx`, plus the
properties that make the boundary real: no host worktree, no state directory or
tmux socket handed to a sandbox, and no secret in an `sbx` argument.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amux import context_service, runtime, sandbox, store, worktree

VERSION_LINE = "sbx version: v0.37.1 2d4f32448c7a94d7fa525517dfca21aa36599829\n"


@pytest.fixture
def minted(monkeypatch):
    """Capture every capability the runtime mints, plaintext included."""
    records: list[dict] = []
    real = store.mint_context_token

    def spy(worktree_id, permissions=(), **kwargs):
        plaintext, token_id = real(worktree_id, permissions=permissions, **kwargs)
        records.append(
            {
                "worktree_id": worktree_id,
                "permissions": tuple(permissions),
                "plaintext": plaintext,
                "token_id": token_id,
            }
        )
        return plaintext, token_id

    monkeypatch.setattr(store, "mint_context_token", spy)
    return records


def make_runtime(**kwargs) -> runtime.SandboxRuntime:
    return runtime.SandboxRuntime(
        runtime.SandboxConfig(
            resources=sandbox.Resources(cpus=2, memory="4g"), port=47317
        ),
        service_healthy=lambda: (True, "ok"),
        **kwargs,
    )


def specs(*panes):
    return [runtime.PaneSpec(p, a, n) for p, a, n in panes]


def respond_ls(fake_sbx, names):
    fake_sbx.respond_json(
        "ls",
        "--json",
        payload={
            "sandboxes": [
                {"name": n, "id": f"sbx_{i}"} for i, n in enumerate(names, start=1)
            ]
        },
    )


HOME = "/home/agent"
HOME_PROBE = ("sh", "-lc", 'printf %s "$HOME"')


def ready(fake_sbx, names, *, codex_version=None):
    """Script a fake `sbx` for a successful creation of `names`.

    Bootstrap asks the VM real questions -- $HOME, the agent's version, whatever
    hook document the image ships -- so the fake has to answer them distinctly
    rather than returning one string for every `exec`. Registered
    most-specific-first, since the fake matches on argv prefix and first match
    wins.
    """
    fake_sbx.respond("version", stdout=VERSION_LINE)
    fake_sbx.respond_json("diagnose", "-o", "json", payload={"checks": []})
    fake_sbx.respond("policy", "check", "network", stdout="allowed\n")
    fake_sbx.respond("create")
    fake_sbx.respond("cp")
    for name in names:
        fake_sbx.respond("exec", name, *HOME_PROBE, stdout=HOME + "\n")
        if codex_version is not None:
            fake_sbx.respond(
                "exec", name, "sh", "-lc", "codex --version 2>/dev/null",
                stdout=codex_version + "\n",
            )
    # Everything else: succeed silently. An image that ships no hook document is
    # the expected case, so an empty `cat` must not look like a failure.
    fake_sbx.respond("exec", stdout="")
    fake_sbx.respond("rm")
    respond_ls(fake_sbx, names)


def names_for(repo, *agent_names):
    return [
        sandbox.sandbox_name("ws", "t0", n, str(repo)) for n in agent_names
    ]


# --- the headline scenario ---


def test_mixed_grid_creates_one_capped_sandbox_per_pane(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    rt = make_runtime()

    launches = rt.prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws",
        task="t0",
        cwd=str(git_repo),
        socket="amux-root",
    )

    creates = [c for c in fake_sbx.calls if c[0] == "create"]
    assert len(creates) == 2
    for call, agent, name in zip(creates, ("claude", "codex"), names, strict=True):
        assert call == [
            "create", "--clone", "--name", name,
            "--cpus", "2", "--memory", "4g", "--no-share-skills",
            agent, str(git_repo),
        ]
    # Each pane attaches to its own sandbox rather than launching an agent.
    assert [l.keys for l in launches] == [
        (f"sbx run --name {names[0]}",),
        (f"sbx run --name {names[1]}",),
    ]


def test_each_agent_gets_its_own_branch_off_the_task_base(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    checkouts = [c for c in fake_sbx.calls if c[:2] == ["exec", names[0]]
                 or c[:2] == ["exec", names[1]]]
    branches = [c[-1] for c in checkouts if "checkout" in c]
    assert branches == ["amux/ws/t0/alpha", "amux/ws/t0/beta"]


def test_registry_rows_record_sandbox_identity(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    rows = {r["name"]: r for r in store.worktrees_for("ws", "t0")}
    assert set(rows) == {"alpha", "beta"}
    for name, row in rows.items():
        assert row["runtime"] == "docker-sandbox"
        assert row["runtime_status"] == "running"
        assert row["sandbox_name"] == sandbox.sandbox_name(
            "ws", "t0", name, str(git_repo)
        )
        assert row["sandbox_id"].startswith("sbx_")
        assert row["branch"] == f"amux/ws/t0/{name}"
        assert row["status"] == "active"
        # A sandbox has no host worktree; nothing may treat this as a directory.
        assert row["path"] == ""
        # Explicit, because "" now means a pre-schema-3 row.
        assert row["socket_name"] == "amux-root"


def test_no_host_worktree_is_created_for_a_sandbox_agent(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    root = Path(worktree.task_worktree_root("ws", "t0"))
    # The shared integration worktree exists; the agent's own does not.
    assert (root / worktree.INTEGRATION_DIR).is_dir()
    assert not (root / "alpha").exists()


def test_the_integration_worktree_is_shared_with_the_host_runtime(git_repo, fake_sbx):
    """Sandbox branches merge back into the same line host agents use."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    (row,) = store.worktrees_for("ws", "t0")
    assert row["base_ref"]
    assert worktree.integration_branch("ws", "t0") == "amux/ws/t0/integration"


# --- capability delivery ---


def test_each_agent_gets_its_own_capability(git_repo, fake_sbx, minted):
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    rows = {r["name"]: r["id"] for r in store.worktrees_for("ws", "t0")}
    assert len(minted) == 2
    # Distinct secrets, each bound to a different execution row.
    assert len({m["plaintext"] for m in minted}) == 2
    assert {m["worktree_id"] for m in minted} == set(rows.values())


def test_permissions_come_from_the_shared_constant(git_repo, fake_sbx, minted):
    """A hand-rolled list would drift from what the routes require and surface
    as a 403 that reads like an auth bug, so use the service's own vocabulary."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    assert minted[0]["permissions"] == tuple(context_service.AGENT_PERMISSIONS)


def test_the_token_never_appears_in_an_sbx_argument(git_repo, fake_sbx, minted):
    """argv is visible to every process on the host, so the secret must travel
    as a file. Only hashes reach SQLite, so the plaintext is captured at the
    mint call to make this assertable at all."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    flat = " ".join(" ".join(call) for call in fake_sbx.calls)
    assert minted
    for record in minted:
        assert record["plaintext"] not in flat


def test_the_sandbox_endpoint_targets_the_host_not_loopback(git_repo, fake_sbx):
    config = runtime.SandboxConfig(port=47317)
    # A sandbox cannot reach the host's 127.0.0.1; Docker routes this name.
    assert config.client_endpoint == "http://host.docker.internal:47317"
    # Policy, however, is written about the loopback address the service binds.
    assert config.policy_target == "localhost:47317"


def test_no_state_directory_or_tmux_socket_is_handed_to_a_sandbox(
    git_repo, fake_sbx, isolate_state
):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    flat = " ".join(" ".join(call) for call in fake_sbx.calls)

    # The database never crosses the boundary in any form.
    assert "context.db" not in flat

    # A host path may legitimately appear as the *source* of `sbx cp` -- that is
    # how the capability is delivered. What must never happen is the state
    # directory becoming reachable *inside* the VM, so check the destinations.
    destinations = [call[2].split(":", 1)[1] for call in fake_sbx.calls if call[0] == "cp"]
    assert destinations  # the shim and its config really were delivered
    for destination in destinations:
        assert str(isolate_state) not in destination
        assert "/worktrees/" not in destination

    # `sbx create` mounts only the repository; no extra workspace is passed.
    (create,) = [call for call in fake_sbx.calls if call[0] == "create"]
    assert create[-1] == str(git_repo)
    assert str(isolate_state) not in " ".join(create)

    # The tmux socket stays on the host: the sandbox coordinates over HTTP.
    assert not any("amux-root" in arg for call in fake_sbx.calls for arg in call)


# --- preflight placement ---


def test_preflight_refuses_a_secondary_worktree_before_creating_anything(
    git_repo, git_run, tmp_path, fake_sbx
):
    ready(fake_sbx, [])
    linked = tmp_path / "linked"
    git_run(git_repo, "worktree", "add", "-q", str(linked), "-b", "side")

    with pytest.raises(sandbox.SandboxError, match="preflight failed"):
        make_runtime().preflight(
            ["claude"], workspace="ws", task="t0", cwd=str(linked)
        )
    assert not fake_sbx.called_with("create")


def test_preflight_refuses_an_unsupported_agent(git_repo, fake_sbx):
    ready(fake_sbx, [])
    with pytest.raises(sandbox.SandboxError, match="preflight failed"):
        make_runtime().preflight(
            ["claude", "gemini"], workspace="ws", task="t0", cwd=str(git_repo)
        )
    assert not fake_sbx.called_with("create")


def test_prepare_refuses_a_non_repository(tmp_path, fake_sbx):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(sandbox.SandboxError, match="not a git repository"):
        make_runtime().prepare(
            specs(("%1", "claude", "alpha")),
            workspace="ws", task="t0", cwd=str(plain), socket="amux-root",
        )


def test_prepare_requires_a_scope(git_repo, fake_sbx):
    with pytest.raises(sandbox.SandboxError, match="workspace, task and path"):
        make_runtime().prepare(
            specs(("%1", "claude", "alpha")),
            workspace=None, task=None, cwd=str(git_repo), socket="amux-root",
        )


# --- attachment ---


def test_attachment_reattaches_rather_than_relaunching(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    (launch,) = make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    # No agent positional: `sbx run --name` resumes the agent already inside.
    assert launch.keys == (f"sbx run --name {names[0]}",)
    assert "claude" not in launch.keys[0]
    # The pane's directory lives in the VM, so the host contributes none.
    assert launch.cwd == ""


# --- both halves of bootstrap ---


def test_both_bootstrap_halves_run(git_repo, fake_sbx):
    """The shim and the capability alone give an agent a working `amux` that
    never reports anything, because state events come from its own hooks. A
    sandbox missing them reads permanently idle, and no offline test can catch
    that from behaviour -- hooks only fire inside a live VM -- so the wiring
    itself is what gets asserted."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )

    # The client half: the shim and its config were delivered.
    delivered = [c[2].split(":", 1)[1] for c in fake_sbx.calls if c[0] == "cp"]
    assert "/usr/local/bin/amux" in delivered
    assert any(d.endswith("/context.json") for d in delivered)

    # The hooks half: the agent's own hook document was written. Asserted by the
    # specific file, not by "some exec happened" -- a trivial probe satisfies
    # that while the real install does nothing.
    hooks = rt.hooks["%1"]
    assert hooks.settings_path.endswith(".claude/settings.json")
    assert any(hooks.settings_path in " ".join(c) for c in fake_sbx.calls)
    assert hooks.mechanism == "hooks"
    # False on purpose, and honest: the image's real hook location has not been
    # inspected in a live VM yet (that is 6.4). Recording "assumed" beats
    # claiming a verification nobody performed.
    assert hooks.location_verified is False


def test_hook_installation_records_what_the_agent_cannot_report(git_repo, fake_sbx):
    """An old Codex has only the single `notify` slot. That is detected in the
    image, and the resulting gap is recorded rather than swallowed."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names, codex_version="codex-cli 0.5.0")
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "codex", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    hooks = rt.hooks["%1"]
    # The version was read from the image, not assumed...
    assert hooks.agent_version == "codex-cli 0.5.0"
    # ...and 0.5.0 predates the full hook surface, so it falls back.
    assert hooks.mechanism == "notify"
    assert hooks.degraded and hooks.missing_kinds


def test_a_current_codex_image_is_not_degraded(git_repo, fake_sbx):
    """The fallback must be chosen by detection, not applied to every Codex."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names, codex_version="codex-cli 0.146.0")
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "codex", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    hooks = rt.hooks["%1"]
    assert hooks.agent_version == "codex-cli 0.146.0"
    assert hooks.mechanism == "hooks"
    assert not hooks.degraded
    assert hooks.settings_path.endswith(".codex/hooks.json")


def test_a_degraded_agent_is_announced_not_swallowed(git_repo, fake_sbx, capsys,
                                                     monkeypatch):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)

    real = runtime.sandbox_bootstrap.install_hooks
    degraded = runtime.sandbox_bootstrap.HooksInstalled(
        agent="codex",
        settings_path="/home/agent/.codex/config.toml",
        missing_kinds=("busy", "notify"),
        location_verified=True,
        agent_version="codex-cli 0.5.0",
        mechanism="notify",
    )
    monkeypatch.setattr(
        runtime.sandbox_bootstrap, "install_hooks", lambda *a, **k: degraded
    )
    assert real is not runtime.sandbox_bootstrap.install_hooks

    rt = make_runtime()
    rt.prepare(
        specs(("%1", "codex", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    out = capsys.readouterr().out
    assert "cannot report" in out
    assert "busy" in out and "notify" in out
    assert "codex-cli 0.5.0" in out
    assert rt.hooks["%1"].degraded
