"""Installing the shim and its capability into a sandbox (`sandbox_bootstrap`).

The bootstrap never shells out to `sbx` itself — it drives an injected
`SandboxOps`, which `sandbox.py` implements over `sbx cp` / `sbx exec`. These
tests use a recording double, so they assert the two properties that matter and
cannot be checked at the `sbx` layer: the plaintext token exists on the host for
the shortest possible time at mode 0600, and it never appears in an argument,
a destination path, or a diagnostic.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
from pathlib import Path

import pytest

from amux import sandbox_bootstrap as sb
from amux import sandbox_client as sc
from amux import shared

ENDPOINT = "http://host.docker.internal:8765"
TOKEN = "zzz-capability-secret-zzz"


class FakeOps:
    """Records what the bootstrap asked the sandbox to do."""

    def __init__(self, name: str = "amux-myproj-fix-brave-hawk-ab12cd", home: str = "/root"):
        self.name = name
        self.home = home
        #: What the image already ships, keyed by absolute in-VM path.
        self.files: dict[str, str] = {}
        #: What `<binary> --version` reports inside the image.
        self.versions: dict[str, str] = {}
        self.copies: list[tuple[Path, str]] = []
        self.execs: list[list[str]] = []
        self.copied: dict[str, bytes] = {}
        self.fail_on: str | None = None
        self.fail_copy: str | None = None

    def copy_in(self, source: Path, destination: str) -> None:
        if self.fail_copy and self.fail_copy in destination:
            raise sb.BootstrapError(
                f"sbx cp {source} {self.name}:{destination} failed: disk full"
            )
        self.copies.append((Path(source), destination))
        self.copied[destination] = Path(source).read_bytes()

    def exec(self, argv):
        argv = list(argv)
        self.execs.append(argv)
        if self.fail_on and self.fail_on in " ".join(argv):
            raise sb.BootstrapError(f"sbx exec {self.name} {' '.join(argv)} failed: rc 1")
        if argv[:2] == ["sh", "-lc"]:
            script = argv[2]
            if script.startswith("cat "):
                # what `_read_optional` runs; "" is a file the image lacks
                path = shlex.split(script)[1]
                return self.files.get(path, "")
            if "--version" in script:
                return self.versions.get(shlex.split(script)[0], "")
            return self.home
        return ""

    # -- assertions helpers ------------------------------------------------

    @property
    def argv_text(self) -> str:
        return " ".join(" ".join(a) for a in self.execs)

    def mode_set_for(self, path: str) -> str | None:
        for argv in self.execs:
            if argv[:1] == ["chmod"] and argv[-1] == path:
                return argv[1]
        return None


@pytest.fixture
def staging(tmp_path):
    return tmp_path / "staging"


# --- staging the capability file ---------------------------------------------


def test_the_staged_config_is_created_readable_only_by_its_owner(staging):
    path = sb.stage_config_file(ENDPOINT, TOKEN, directory=staging)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"endpoint": ENDPOINT, "token": TOKEN}


def test_the_staged_config_is_never_briefly_world_readable(staging, monkeypatch):
    """Written through O_CREAT|O_EXCL with the mode up front, not chmod-ed after:
    a chmod leaves a window in which another local user can read the token."""
    modes: list[int] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        if str(path).endswith("context.json"):
            modes.append(mode)
            assert flags & os.O_EXCL, "must refuse to reuse an existing staging file"
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    sb.stage_config_file(ENDPOINT, TOKEN, directory=staging)
    assert modes == [0o600]


def test_staging_refuses_to_overwrite_an_existing_file(staging):
    sb.stage_config_file(ENDPOINT, TOKEN, directory=staging)
    with pytest.raises(sb.BootstrapError):
        sb.stage_config_file(ENDPOINT, TOKEN, directory=staging)


def test_the_staged_config_is_what_the_client_then_loads(staging, monkeypatch):
    """The two halves of the contract are one file format, not two."""
    path = sb.stage_config_file(ENDPOINT, TOKEN, directory=staging)
    monkeypatch.setenv(sc.CONFIG_ENV, str(path))
    config = sc.load_config()
    assert (config.endpoint, config.token) == (ENDPOINT, TOKEN)


# --- installing into the sandbox ---------------------------------------------


def test_install_copies_the_shim_and_the_capability_and_locks_both_down(staging):
    ops = FakeOps()
    result = sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)

    assert result.shim_path == sb.SHIM_PATH
    assert result.config_path == "/root/.config/amux/context.json"
    destinations = [dest for _, dest in ops.copies]
    assert sb.SHIM_PATH in destinations
    assert result.config_path in destinations
    assert ops.mode_set_for(result.config_path) == "600"
    assert ops.mode_set_for(sb.SHIM_PATH) == "755"


def test_install_delivers_the_client_source_verbatim(staging):
    ops = FakeOps()
    sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert ops.copied[sb.SHIM_PATH] == Path(sc.__file__ or "").read_bytes()


def test_install_delivers_a_config_the_shim_can_read(staging):
    ops = FakeOps()
    result = sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert json.loads(ops.copied[result.config_path]) == {
        "endpoint": ENDPOINT,
        "token": TOKEN,
    }


def test_install_resolves_the_agents_own_home_rather_than_assuming_root(staging):
    ops = FakeOps(home="/home/agent")
    result = sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert result.config_path == "/home/agent/.config/amux/context.json"
    assert ["mkdir", "-p", "/home/agent/.config/amux"] in ops.execs


def test_install_creates_the_config_directory_before_copying_into_it(staging):
    ops = FakeOps()
    sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    mkdir_index = ops.execs.index(["mkdir", "-p", "/root/.config/amux"])
    chmod_index = next(
        i for i, a in enumerate(ops.execs) if a[:1] == ["chmod"] and a[-1].endswith("context.json")
    )
    assert mkdir_index < chmod_index


# --- the token must not leak --------------------------------------------------


def test_the_token_never_appears_in_an_argument_or_a_destination_path(staging):
    ops = FakeOps()
    sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    # An install that did nothing would satisfy every `not in` below, so prove
    # the work happened AND that the token really did travel — as file contents.
    assert len(ops.copies) == 2 and ops.execs
    assert TOKEN in ops.copied[f"{ops.home}/.config/amux/context.json"].decode()

    assert TOKEN not in ops.argv_text
    assert not any(TOKEN in dest for _, dest in ops.copies)
    assert not any(TOKEN in str(src) for src, _ in ops.copies)


def test_the_host_staging_file_is_gone_once_the_sandbox_has_it(staging):
    ops = FakeOps()
    sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert list(staging.rglob("*.json")) == []


@pytest.mark.parametrize(
    "failure", ["mkdir", "chmod 600", "amux --help"], ids=["mkdir", "chmod", "verify"]
)
def test_a_failed_exec_still_removes_the_plaintext_from_the_host(staging, failure):
    ops = FakeOps()
    ops.fail_on = failure
    with pytest.raises(sb.BootstrapError):
        sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert list(staging.rglob("*.json")) == []


def test_a_failed_copy_still_removes_the_plaintext_from_the_host(staging):
    ops = FakeOps()
    ops.fail_copy = "context.json"
    with pytest.raises(sb.BootstrapError):
        sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert list(staging.rglob("*.json")) == []


def test_a_failure_diagnostic_names_the_command_but_redacts_the_capability(staging):
    ops = FakeOps()
    ops.fail_on = "mkdir"
    with pytest.raises(sb.BootstrapError) as failure:
        sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    message = str(failure.value)
    assert "mkdir" in message  # the operator still learns what broke
    assert TOKEN not in message
    assert ENDPOINT in message or ops.name in message


def test_a_diagnostic_that_would_have_echoed_the_token_is_redacted(staging):
    """Whatever the transport puts in an error, this layer knows the secret and
    is the last place able to scrub it."""
    ops = FakeOps()

    def leaky(argv):
        raise sb.BootstrapError(f"boom: wrote {TOKEN} to disk")

    ops.exec = leaky  # type: ignore[method-assign]
    with pytest.raises(sb.BootstrapError) as failure:
        sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert TOKEN not in str(failure.value)
    assert "***" in str(failure.value)


def test_the_staging_directory_defaults_under_the_amux_state_directory(tmp_path, monkeypatch):
    """Patched via `shared`, which is also the proof that conftest's
    `isolate_state` reaches this module: binding STATE_DIR at import would put
    real capability files in the live state directory during tests."""
    monkeypatch.setattr(shared, "STATE_DIR", tmp_path / "amux")
    assert sb.default_staging_dir() == tmp_path / "amux" / "staging"


def test_install_never_writes_the_capability_into_the_sandbox_environment(staging):
    """An env var is visible to every process in the VM and to `sbx` inspection;
    the token belongs in one 0600 file the shim opens."""
    ops = FakeOps()
    result = sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    # Guard on the install's own commands, not merely on `execs` being non-empty:
    # the $HOME probe alone would satisfy that while the install did nothing.
    assert ops.mode_set_for(result.config_path) == "600"
    assert ops.mode_set_for(sb.SHIM_PATH) == "755"
    for argv in ops.execs:
        assert not any(arg.startswith("AMUX_CONTEXT_TOKEN") for arg in argv)
        assert "export" not in " ".join(argv)


# --- what the sandbox must NOT receive ---------------------------------------


def test_nothing_the_bootstrap_copies_comes_from_the_host_state_directory(staging):
    ops = FakeOps()
    sb.install_client(ops, endpoint=ENDPOINT, token=TOKEN, staging_dir=staging)
    assert ops.copies and ops.execs, "nothing was copied or run to inspect"
    for source, _ in ops.copies:
        text = str(source)
        assert source.name in ("context.json", "sandbox_client.py")  # only these two
        assert "context.db" not in text
        assert not text.endswith(".db")
    assert not any("context.db" in " ".join(a) for a in ops.execs)
    assert not any("/tmp/tmux" in " ".join(a) for a in ops.execs)
