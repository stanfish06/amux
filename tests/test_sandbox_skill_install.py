"""Installing amux's own skill into a sandbox (`sandbox_bootstrap.install_skill`).

A sandbox is created with `--no-share-skills`, so the host's skill directory is
not visible inside it. `skills/amux/SKILL.md` is the document that tells an agent
which commands cross the host boundary, which makes its absence a real gap: the
agent has to discover the boundary by tripping over it. These tests pin the two
properties that gap needs — the skill is delivered where the agent looks for it,
and a failure to deliver it degrades the spawn instead of failing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amux import sandbox_bootstrap as sb
from amux import sandbox_hooks
from test_sandbox_client_bootstrap import ENDPOINT, TOKEN, FakeOps


@pytest.fixture
def installed(tmp_path):
    """A sandbox that has already had the shim and capability installed."""
    return sb.install_client(
        FakeOps(), endpoint=ENDPOINT, token=TOKEN, staging_dir=tmp_path / "staging"
    )


# --- finding the document to install -----------------------------------------


def test_the_skill_resolves_to_the_one_copy_in_the_repository():
    """Not a duplicate under src/: the Makefile links this same file on a host."""
    source = sb.skill_source()
    assert source.is_file()
    assert source.parts[-3:] == ("skills", "amux", "SKILL.md")
    assert source.read_text().startswith("---\nname: amux\n")


def test_a_packaged_amux_finds_the_skill_beside_the_package(tmp_path, monkeypatch):
    """What `pyinstaller --add-data ...:amux/skills/amux` produces: no repo root."""
    package = tmp_path / "_internal" / "amux"
    bundled = package / "skills" / "amux" / "SKILL.md"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("---\nname: amux\n---\n")
    monkeypatch.setattr(
        "amux.sandbox_client.__file__", str(package / "sandbox_client.py")
    )
    assert sb.skill_source() == bundled


def test_a_missing_skill_names_both_places_it_looked_and_how_to_ship_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "amux.sandbox_client.__file__", str(tmp_path / "a" / "b" / "sandbox_client.py")
    )
    with pytest.raises(sb.BootstrapError) as exc:
        sb.skill_source()
    message = str(exc.value)
    assert "--add-data" in message
    # Both candidates: beside the package, and the repo root two levels up
    # (`src/amux` -> the checkout), which is what `a/b` stands in for here.
    assert str(tmp_path / "a" / "b" / "skills" / "amux" / "SKILL.md") in message
    assert str(tmp_path / "skills" / "amux" / "SKILL.md") in message


# --- delivering it into the sandbox ------------------------------------------


def test_the_skill_lands_where_claude_discovers_skills(installed):
    ops = FakeOps()
    result = sb.install_skill(ops, "claude", installed)

    assert result.ok
    assert result.path == "/root/.claude/skills/amux/SKILL.md"
    assert ops.copied[result.path] == sb.skill_source().read_bytes()


def test_codex_gets_the_same_document_in_its_own_skill_directory(installed):
    ops = FakeOps()
    result = sb.install_skill(ops, "codex", installed)

    assert result.path == "/root/.codex/skills/amux/SKILL.md"
    assert ops.copied[result.path] == sb.skill_source().read_bytes()


def test_every_agent_adapter_declares_where_its_skills_live():
    """A new adapter must not silently inherit claude's path."""
    for agent, hooks in sandbox_hooks.HOOKS_BY_AGENT.items():
        assert hooks.skills_relpath.startswith(f".{agent}/"), agent
        assert hooks.skills_relpath.endswith("/skills"), agent


def test_the_skill_directory_is_created_before_the_copy(installed):
    ops = FakeOps()
    result = sb.install_skill(ops, "claude", installed)

    assert ops.index_of("mkdir", "-p", "/root/.claude/skills/amux") < ops.index_of(
        "chmod", sb.SKILL_MODE, result.path
    )


def test_the_skill_is_world_readable_and_owned_by_the_agent(installed):
    """644, not the shim's 755: an agent reads this document, it does not run it."""
    ops = FakeOps()
    result = sb.install_skill(ops, "claude", installed)

    assert ops.mode_set_for(result.path) == "644"
    assert ops.owners[result.path] == ops.whoami
    # chown as root must precede chmod, or the agent cannot chmod a host-owned file.
    assert ops.user_for("chown") == "root"
    assert ops.index_of("chown") < ops.index_of("chmod", "644", result.path)


# --- failing without failing the spawn ---------------------------------------


def test_a_missing_source_degrades_the_spawn_rather_than_aborting_it(
    installed, tmp_path, monkeypatch
):
    """The shim and its capability make an agent work; this document only informs
    it. Losing a markdown file must not take down a whole grid."""
    monkeypatch.setattr(
        "amux.sandbox_client.__file__", str(tmp_path / "a" / "b" / "sandbox_client.py")
    )
    ops = FakeOps()
    result = sb.install_skill(ops, "claude", installed)

    assert not result.ok
    assert "SKILL.md" in result.reason
    assert ops.copies == []


def test_a_copy_failure_is_reported_not_raised(installed):
    ops = FakeOps()
    ops.fail_copy = "SKILL.md"
    result = sb.install_skill(ops, "claude", installed)

    assert not result.ok
    assert "SKILL.md" in result.reason


def test_an_unknown_agent_is_reported_not_raised(installed):
    result = sb.install_skill(FakeOps(), "gemini", installed)

    assert not result.ok
    assert "gemini" in result.reason


def test_the_reason_never_carries_the_capability_token(installed):
    ops = FakeOps()
    ops.fail_copy = "SKILL.md"
    result = sb.install_skill(ops, "claude", installed)

    assert TOKEN not in result.reason
