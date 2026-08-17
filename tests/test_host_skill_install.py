"""Installing amux's own skill onto the host (`install_host_skill`).

A host agent's copy of `skills/amux/SKILL.md` used to arrive only if someone had
run `make install_skills`, so an amux installed any other way left the agent with
no document and no way to know it was missing one. Spawning now writes it.

Two properties carry the risk. The write goes into `$HOME`, a directory the user
curates by hand, so it must land at exactly one named path and must never be
followed through the symlink `make install_skills` leaves into a checkout. And it
must degrade: losing a markdown file cannot cost anyone their grid.

Every test injects `home`, so none of them touch a real `$HOME`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from amux import sandbox_bootstrap as sb


@pytest.fixture
def home(isolate_home):
    """Alias: these tests inject `home` explicitly rather than reading `$HOME`.

    It is the same directory conftest redirects `$HOME` to, so a test that
    forgot to inject would still not reach the developer's own skill directory.
    """
    return isolate_home


@pytest.fixture
def checkout(tmp_path):
    """A stand-in amux checkout, as `make install_skills` would link to it."""
    skill = tmp_path / "checkout" / "skills" / "amux" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: amux\n---\nthe checkout copy\n")
    return skill


# --- where it lands ----------------------------------------------------------


def test_claude_gets_the_document_in_the_directory_it_reads(home):
    result = sb.install_host_skill("claude", home=home)

    assert result.ok
    assert result.path == str(home / ".claude" / "skills" / "amux" / "SKILL.md")
    assert (home / ".claude/skills/amux/SKILL.md").read_bytes() == (
        sb.skill_source().read_bytes()
    )


def test_codex_gets_the_same_document_in_its_own_directory(home):
    result = sb.install_host_skill("codex", home=home)

    assert result.path == str(home / ".codex" / "skills" / "amux" / "SKILL.md")
    assert (home / ".codex/skills/amux/SKILL.md").read_bytes() == (
        sb.skill_source().read_bytes()
    )


def test_the_whole_path_is_created_when_nothing_exists_yet(home):
    """No prerequisite step: not the skills dir, not the agent's dot-directory."""
    assert not (home / ".claude").exists()

    result = sb.install_host_skill("claude", home=home)

    assert result.ok
    assert result.changed


def test_the_document_is_readable_and_not_executable(home):
    """644, as in a sandbox: an agent reads this document, it does not run it."""
    result = sb.install_host_skill("claude", home=home)

    mode = stat.S_IMODE(os.stat(result.path).st_mode)
    assert oct(mode) == oct(int(sb.SKILL_MODE, 8))


def test_an_unknown_agent_has_no_skill_directory_to_install_into(home):
    result = sb.install_host_skill("gemini", home=home)

    assert not result.ok
    assert "gemini" in result.reason


# --- overwriting what is there -----------------------------------------------


def test_a_stale_copy_is_replaced_and_the_replacement_is_reported(home):
    destination = home / ".claude" / "skills" / "amux" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("an older amux wrote this\n")

    result = sb.install_host_skill("claude", home=home)

    assert result.changed
    assert destination.read_bytes() == sb.skill_source().read_bytes()


def test_a_second_spawn_changes_nothing_and_says_so(home):
    """`changed` is what keeps a no-op spawn from printing a path every time."""
    first = sb.install_host_skill("claude", home=home)
    second = sb.install_host_skill("claude", home=home)

    assert first.changed
    assert not second.changed
    assert second.path == first.path
    assert (home / ".claude/skills/amux/SKILL.md").read_bytes() == (
        sb.skill_source().read_bytes()
    )


def test_a_symlinked_skill_directory_is_replaced_by_a_real_one(home, checkout):
    """What `make install_skills` leaves behind: `~/.claude/skills/amux` is a link."""
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "amux").symlink_to(checkout.parent)

    result = sb.install_host_skill("claude", home=home)

    assert result.ok
    assert result.changed
    assert not (skills / "amux").is_symlink()
    assert (skills / "amux").is_dir()
    assert (skills / "amux" / "SKILL.md").read_bytes() == sb.skill_source().read_bytes()


def test_the_checkout_copy_behind_a_symlinked_directory_is_untouched(home, checkout):
    """The developer's working tree is not amux's to edit. 1.6."""
    before = checkout.read_bytes()
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "amux").symlink_to(checkout.parent)

    sb.install_host_skill("claude", home=home)

    assert checkout.is_file()
    assert checkout.read_bytes() == before


def test_a_symlinked_document_is_replaced_rather_than_written_through(home, checkout):
    """The same hazard one level down: only `SKILL.md` itself is the link."""
    before = checkout.read_bytes()
    destination = home / ".claude" / "skills" / "amux" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(checkout)

    result = sb.install_host_skill("claude", home=home)

    assert result.changed
    assert not destination.is_symlink()
    assert destination.read_bytes() == sb.skill_source().read_bytes()
    assert checkout.read_bytes() == before


def test_a_symlink_pointing_at_an_identical_file_is_still_replaced(home, tmp_path):
    """Same bytes is not the same thing: the next write must not reach the target."""
    twin = tmp_path / "twin.md"
    twin.write_bytes(sb.skill_source().read_bytes())
    destination = home / ".claude" / "skills" / "amux" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(twin)

    result = sb.install_host_skill("claude", home=home)

    assert result.changed
    assert not destination.is_symlink()


# --- failing without failing the spawn ---------------------------------------


def test_an_unwritable_destination_is_reported_not_raised(home):
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    skills.chmod(0o500)
    try:
        result = sb.install_host_skill("claude", home=home)
    finally:
        skills.chmod(0o700)

    assert not result.ok
    assert result.reason


def test_an_unresolvable_source_document_is_reported_not_raised(
    home, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "amux.sandbox_client.__file__", str(tmp_path / "a" / "b" / "sandbox_client.py")
    )

    result = sb.install_host_skill("claude", home=home)

    assert not result.ok
    assert "SKILL.md" in result.reason
    assert not (home / ".claude").exists()


def test_an_unresolvable_home_is_reported_not_raised(home, monkeypatch):
    """`Path.home()` raises `RuntimeError` where its pwd fallback also fails.

    Not an `OSError`, so a named catch list misses it -- and this call sits
    inside `HostRuntime.prepare`, where anything raising is converted into a
    `GridCreationError` *with runtime rollback*. Under `docker-sandbox` that
    destroys the grid's microVMs, over a markdown file.
    """

    def no_home() -> object:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr("pathlib.Path.home", staticmethod(no_home))

    result = sb.install_host_skill("claude")

    assert not result.ok
    assert "home directory" in result.reason


def test_a_failure_with_an_empty_message_still_reports_something(home, monkeypatch):
    """`str(exc)` is empty for a bare raise; a blank reason reads as no reason."""

    def blow_up(*_args, **_kwargs) -> object:
        raise RuntimeError

    monkeypatch.setattr(sb, "host_skill_destination", blow_up)

    result = sb.install_host_skill("claude", home=home)

    assert not result.ok
    assert result.reason == "RuntimeError"


def test_an_injected_source_is_what_gets_written(home, checkout):
    """`source` is how the sandbox path and tests avoid re-resolving the document."""
    result = sb.install_host_skill("claude", home=home, source=checkout)

    assert result.ok
    assert (home / ".claude/skills/amux/SKILL.md").read_bytes() == checkout.read_bytes()


# --- the documented recovery ---------------------------------------------------


def test_make_install_skills_restores_the_link_this_install_replaced(home):
    """The one mitigation the design offers for breaking the dogfooding loop.

    `README`, `design.md` and the `install_skills` comment all promise that
    re-running the target turns the frozen copy back into a live checkout link.
    That promise was false: `ln -sfn` replaces a *symlink* to a directory but
    does nothing when the destination IS a real directory, which after an install
    it always is -- it linked *inside* it instead, exited 0, and reported
    success while the agent kept reading the frozen copy.

    Asserted by running the target, not by reading it: the failure was silent
    and self-congratulating, so only the resulting inode settles it.
    """
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    repo = Path(__file__).resolve().parents[1]
    destination = home / ".claude" / "skills" / "amux"

    def install_skills() -> None:
        # `HOME=` twice: as a make override, which wins over the environment, and
        # in the environment too, so a mechanism change cannot silently redirect
        # this at the developer's own skill directory.
        subprocess.run(
            (make, "-s", "-C", str(repo), "install_skills", f"HOME={home}"),
            env={**os.environ, "HOME": str(home)},
            capture_output=True,
            check=True,
        )

    install_skills()
    assert destination.is_symlink()

    sb.install_host_skill("claude", home=home)
    assert not destination.is_symlink() and destination.is_dir()

    install_skills()

    assert destination.is_symlink()
    assert destination.resolve() == (repo / "skills" / "amux").resolve()
    # The shape the old target produced: a live-looking link nested one level
    # down, inside a real directory the agent never reads through.
    assert not (destination / "amux").exists()
