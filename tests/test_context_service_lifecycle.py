"""Task 2.5: serve, start, status, stop — and every way they refuse.

These tests run the service as a real child process rather than a thread. The
run file, the pid, the signal handling and the port conflict are all
process-level facts, and a thread cannot be honest about any of them.

`isolate_state` already points `XDG_STATE_HOME` at a per-test directory, so a
child inheriting this environment resolves the same state directory and the
same store — and never the live one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from amux import context_service as cs
from amux import store


SERVE = [sys.executable, "-m", "amux.context_service"]


@pytest.fixture
def config(isolate_state, db_path):
    """A configuration whose state directory is the isolated one, with an
    ephemeral port so parallel runs cannot collide."""
    store.schema_version(db_path)  # create the store, as a native command would
    return cs.ServiceConfig(port=0, db_path=db_path, state_dir=isolate_state)


@pytest.fixture
def children():
    """Every process a test spawned, killed on the way out."""
    spawned: list[subprocess.Popen] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
            child.wait(10)


def _spawn(children, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    child = subprocess.Popen(
        [*SERVE, *args],
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    children.append(child)
    return child


def _await(predicate, timeout: float = 15.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*SERVE, *args],
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


# --- the run file ---


def test_the_run_file_lives_under_the_state_directory(config):
    assert cs.runfile_path(config) == config.state_home / cs.RUNFILE_NAME
    assert cs.runfile_path(cs.ServiceConfig()).parent == cs.ServiceConfig().state_home


def test_a_run_file_round_trips(config):
    run = cs.RunFile(pid=4242, port=47317, started_ts=1000.5, schema_version=3)
    cs.write_runfile(config, run)
    assert cs.read_runfile(config) == run
    assert json.loads(cs.runfile_path(config).read_text())["pid"] == 4242


def test_an_unreadable_run_file_reads_as_none(config):
    cs.runfile_path(config).parent.mkdir(parents=True, exist_ok=True)
    for junk in ("", "not json", '{"port": 1}', '{"pid": "x", "port": 1}'):
        cs.runfile_path(config).write_text(junk)
        assert cs.read_runfile(config) is None
        # ...and a corrupt file is never mistaken for a running service.
        assert cs.status(config).state == "stopped"


def test_removing_a_run_file_respects_its_owner(config):
    cs.write_runfile(config, cs.RunFile(pid=111, port=1, started_ts=1.0))
    cs.remove_runfile(config, only_pid=222)
    assert cs.read_runfile(config) is not None  # not ours to delete
    cs.remove_runfile(config, only_pid=111)
    assert cs.read_runfile(config) is None
    cs.remove_runfile(config)  # idempotent


# --- status ---


def test_status_of_nothing(config):
    result = cs.status(config)
    assert result.state == "stopped"
    assert result.running is False
    assert "not running" in result.message


def test_status_detects_a_stale_run_file(config):
    dead = _dead_pid()
    cs.write_runfile(config, cs.RunFile(pid=dead, port=47317, started_ts=1.0))
    result = cs.status(config)
    assert result.state == "stale"
    assert result.running is False
    assert str(dead) in result.message
    assert "gone" in result.message


def test_status_detects_a_live_process_that_does_not_answer(config):
    """Our own pid is alive and is certainly not serving on that port."""
    cs.write_runfile(config, cs.RunFile(pid=os.getpid(), port=_free_port(), started_ts=1.0))
    result = cs.status(config)
    assert result.state == "unresponsive"
    assert str(os.getpid()) in result.message
    assert "stop it before starting another" in result.message


def _dead_pid() -> int:
    """A pid that has certainly exited."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _free_port() -> int:
    import socket as _socket

    with _socket.socket() as sock:
        sock.bind((cs.LOOPBACK, 0))
        return int(sock.getsockname()[1])


# --- serve ---


def test_serve_writes_a_run_file_and_answers(config, children):
    _spawn(children, "serve", "--port", "0")
    run = _await(lambda: cs.read_runfile(config), what="a run file")
    assert run.pid > 0
    assert run.port > 0
    assert run.schema_version == store.SCHEMA_VERSION
    health = _await(lambda: cs.probe_health(run.port), what="a health answer")
    assert health["ok"] is True
    assert cs.status(config).state == "running"


def test_serve_removes_its_run_file_on_sigterm(config, children):
    child = _spawn(children, "serve", "--port", "0")
    run = _await(lambda: cs.read_runfile(config), what="a run file")
    child.terminate()
    assert child.wait(20) == 0
    assert cs.read_runfile(config) is None
    assert cs.probe_health(run.port, timeout=1.0) is None
    assert cs.status(config).state == "stopped"


def test_serve_logs_under_the_state_directory_and_not_to_the_live_one(config, children):
    _spawn(children, "serve", "--port", "0")
    _await(lambda: cs.read_runfile(config), what="a run file")
    log = _await(
        lambda: config.log_file.exists() and config.log_file.read_text(),
        what="a log file",
    )
    assert "serving on 127.0.0.1" in log
    assert config.log_file.parent == config.state_home


def test_serve_refuses_a_busy_port_without_a_second_listener(config, children):
    first = _spawn(children, "serve", "--port", "0")
    run = _await(lambda: cs.read_runfile(config), what="a run file")

    second = _run("serve", "--port", str(run.port))
    assert second.returncode == 1
    assert "cannot bind 127.0.0.1" in second.stderr + config.log_file.read_text()
    # The original is untouched and still the only listener.
    assert cs.read_runfile(config) == run
    assert first.poll() is None
    assert cs.probe_health(run.port)["ok"] is True


def test_serve_refuses_a_newer_schema_and_binds_nothing(config):
    conn = sqlite3.connect(config.database)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 9}")
    conn.close()
    port = _free_port()

    result = _run("serve", "--port", str(port))
    assert result.returncode == 1
    message = result.stderr + config.log_file.read_text()
    assert str(store.SCHEMA_VERSION + 9) in message
    assert "Upgrade amux" in message
    # Nothing was bound, and nothing claims to be running.
    assert cs.probe_health(port, timeout=1.0) is None
    assert cs.read_runfile(config) is None


def test_serve_refuses_an_unopenable_store(config):
    # The sidecars have to go too: with a valid -wal and -shm beside it, sqlite
    # reads the schema straight back out of them and the garbage main file is
    # never noticed.
    for suffix in ("-wal", "-shm"):
        config.database.with_name(config.database.name + suffix).unlink(missing_ok=True)
    config.database.write_text("this is not a database")
    assert cs.schema_info(config.database).version is None
    port = _free_port()
    result = _run("serve", "--port", str(port))
    assert result.returncode == 1
    assert "cannot open the context store" in result.stderr + config.log_file.read_text()
    assert cs.probe_health(port, timeout=1.0) is None
    assert cs.read_runfile(config) is None


# --- start ---


def test_start_launches_a_service_and_reports_it(config, children):
    result = cs.start(config, launcher=lambda c: _spawn(children, "serve", "--port", "0"))
    assert result.state == "running"
    assert result.healthy
    assert result.port and result.pid
    assert f"{cs.LOOPBACK}:{result.port}" in result.message
    assert cs.probe_health(result.port)["ok"] is True


def test_start_is_idempotent(config, children):
    launches: list[int] = []

    def launcher(cfg):
        launches.append(1)
        return _spawn(children, "serve", "--port", "0")

    first = cs.start(config, launcher=launcher)
    second = cs.start(config, launcher=launcher)
    third = cs.start(config, launcher=launcher)
    assert len(launches) == 1  # preflight calls this on every spawn
    assert (second.pid, second.port) == (first.pid, first.port)
    assert third.state == "running"


def test_start_clears_a_stale_run_file(config, children):
    cs.write_runfile(config, cs.RunFile(pid=_dead_pid(), port=47317, started_ts=1.0))
    result = cs.start(config, launcher=lambda c: _spawn(children, "serve", "--port", "0"))
    assert result.state == "running"
    assert result.pid != 0
    assert cs.read_runfile(config).pid == result.pid


def test_start_refuses_to_stand_up_a_second_service_beside_a_confused_one(config):
    """A live pid that does not answer is a problem to look at, not to double."""
    cs.write_runfile(config, cs.RunFile(pid=os.getpid(), port=_free_port(), started_ts=1.0))
    launched = []
    with pytest.raises(cs.ServiceLifecycleError) as caught:
        cs.start(config, launcher=lambda c: launched.append(1))
    assert launched == []
    assert "stop it before starting another" in str(caught.value)


def test_start_refuses_an_incompatible_schema_before_launching_anything(config):
    conn = sqlite3.connect(config.database)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 9}")
    conn.close()
    launched = []
    with pytest.raises(cs.ServiceStartupError) as caught:
        cs.start(config, launcher=lambda c: launched.append(1))
    assert launched == []
    assert str(store.SCHEMA_VERSION + 9) in str(caught.value)
    assert cs.read_runfile(config) is None


def test_start_gives_up_with_the_log_path_when_nothing_comes_up(config):
    with pytest.raises(cs.ServiceStartupError) as caught:
        cs.start(config, launcher=lambda c: None, timeout=0.5)
    assert str(config.log_file) in str(caught.value)
    assert cs.status(config).state == "stopped"


# --- stop ---


def test_stop_signals_the_service_and_clears_the_run_file(config, children):
    started = cs.start(config, launcher=lambda c: _spawn(children, "serve", "--port", "0"))
    result = cs.stop(config)
    assert result.state == "stopped"
    assert str(started.pid) in result.message
    assert cs.read_runfile(config) is None
    assert cs.probe_health(started.port, timeout=1.0) is None


def test_stop_is_idempotent(config, children):
    cs.start(config, launcher=lambda c: _spawn(children, "serve", "--port", "0"))
    assert cs.stop(config).state == "stopped"
    assert cs.stop(config).state == "stopped"
    assert cs.stop(config).state == "stopped"


def test_stop_clears_a_stale_run_file(config):
    dead = _dead_pid()
    cs.write_runfile(config, cs.RunFile(pid=dead, port=47317, started_ts=1.0))
    result = cs.stop(config)
    assert result.state == "stopped"
    assert "stale" in result.message
    assert cs.read_runfile(config) is None


def test_stop_reports_a_process_that_will_not_go(config, children):
    """A service that ignores SIGTERM leaves the run file alone: a record
    claiming nothing is running while something is would be worse."""
    ready = config.state_home / "ignoring-sigterm"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            # The ready file is not a nicety: signalling before SIG_IGN is
            # installed would kill it, and the test would pass for the wrong
            # reason.
            "import pathlib, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "pathlib.Path(sys.argv[1]).write_text('ready')\n"
            "time.sleep(120)\n",
            str(ready),
        ],
        start_new_session=True,
    )
    children.append(child)
    _await(ready.exists, what="the child to ignore SIGTERM")
    port = _free_port()
    cs.write_runfile(config, cs.RunFile(pid=child.pid, port=port, started_ts=1.0))
    # Unresponsive, so stop() must still be willing to signal it.
    with pytest.raises(cs.ServiceLifecycleError) as caught:
        cs.stop(config, timeout=1.0)
    assert "did not stop" in str(caught.value)
    assert "force" in str(caught.value)
    assert cs.read_runfile(config) is not None

    result = cs.stop(config, force=True, timeout=20.0)
    assert result.state == "stopped"
    assert cs.read_runfile(config) is None


# --- the module entry point ---


def test_status_reports_stopped_on_the_command_line(config):
    result = _run("status")
    assert result.returncode == 0
    assert "not running" in result.stdout


def test_the_command_line_walks_start_status_stop(config, children):
    started = cs.start(config, launcher=lambda c: _spawn(children, "serve", "--port", "0"))

    status_out = _run("status")
    assert status_out.returncode == 0
    assert str(started.port) in status_out.stdout
    assert str(started.pid) in status_out.stdout

    stop_out = _run("stop")
    assert stop_out.returncode == 0
    assert "stopped" in stop_out.stdout
    assert cs.read_runfile(config) is None


def test_a_bad_port_on_the_command_line_is_an_error_not_a_traceback(config):
    result = _run("serve", "--port", "99999")
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "65535" in result.stderr


def test_the_port_can_come_from_the_environment(config, children):
    port = _free_port()
    _spawn(children, "serve", env={cs.ENV_PORT: str(port)})
    run = _await(lambda: cs.read_runfile(config), what="a run file")
    assert run.port == port


def test_an_unknown_action_is_rejected(config):
    result = _run("restart")
    assert result.returncode == 2  # argparse
    assert "invalid choice" in result.stderr


# --- no weaker fallback exists ---


def test_there_is_no_way_to_disable_authentication(config):
    """The refusals above only mean something if there is no unauthenticated
    mode to fall back to."""
    assert cs.ContextService(config).authenticator is cs.store_authenticator
    for name in cs.ServiceConfig.__dataclass_fields__:
        assert "auth" not in name and "insecure" not in name


def test_a_service_started_by_serve_still_demands_a_token(config, children):
    _spawn(children, "serve", "--port", "0")
    run = _await(lambda: cs.read_runfile(config), what="a run file")
    _await(lambda: cs.probe_health(run.port), what="a health answer")

    import http.client

    conn = http.client.HTTPConnection(cs.LOOPBACK, run.port, timeout=5)
    try:
        conn.request("GET", "/v1/context")
        response = conn.getresponse()
        payload = json.loads(response.read())
    finally:
        conn.close()
    assert response.status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_the_run_file_never_names_a_port_nothing_is_listening_on(config, children):
    """The claim order — schema, bind, then run file — is what makes this true."""
    _spawn(children, "serve", "--port", "0")
    run = _await(lambda: cs.read_runfile(config), what="a run file")
    assert cs.probe_health(run.port, timeout=5.0) is not None
