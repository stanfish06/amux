"""Recording libtmux doubles.

`core._build_grid` is the one function that mixes tmux mutation, git worktree
setup, and agent launch. To refactor it behind a runtime seam without changing
host behavior we need to see *every* side effect it performs, in order. These
doubles record each `cmd`/`split`/`set_hook`/`send_keys` call and answer
`show-options` from whatever `set-option` stored, which is enough for
`_taken_names` and `label_for` to work exactly as they do against real tmux.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeResult:
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)


class FakeServer:
    def __init__(self, socket_name: str = "amux-test"):
        self.socket_name = socket_name
        self.log: list[tuple] = []
        self._next_pane = 0

    def pane_id(self) -> str:
        self._next_pane += 1
        return f"%{self._next_pane}"


class FakeSession:
    def __init__(self, server: FakeServer, name: str = "ws"):
        self.server = server
        self.name = name
        self.windows: list[FakeWindow] = []


class FakeWindow:
    def __init__(self, session: FakeSession, name: str = "t0"):
        self.session = session
        self.server = session.server
        self.name = name
        self.panes: list[FakePane] = [FakePane(self)]
        session.windows.append(self)

    def new_pane(self) -> FakePane:
        pane = FakePane(self)
        self.panes.append(pane)
        return pane

    def cmd(self, *args: str) -> FakeResult:
        self.server.log.append(("window-cmd", self.name, *args))
        return FakeResult()


class FakePane:
    def __init__(self, window: FakeWindow, current_path: str = "/pane/current/path"):
        self.window = window
        self.server = window.server
        self.id = window.server.pane_id()
        self.pane_current_path = current_path
        self.options: dict[str, str] = {}

    @property
    def log(self) -> list[tuple]:
        return self.window.server.log

    def cmd(self, *args: str) -> FakeResult:
        self.log.append(("cmd", self.id, *args))
        if args[0] == "show-options":
            value = self.options.get(args[-1])
            return FakeResult(stdout=[value] if value is not None else [])
        if args[0] == "set-option":
            self.options[args[-2]] = args[-1]
        return FakeResult()

    def split(self, direction, size: str, start_directory: str | None) -> FakePane:
        self.log.append(("split", self.id, str(direction), size, start_directory))
        return self.window.new_pane()

    def set_hook(self, name: str, command: str) -> None:
        self.log.append(("set_hook", self.id, name, command))

    def send_keys(self, keys: str) -> None:
        self.log.append(("send_keys", self.id, keys))


def new_window(socket_name: str = "amux-test") -> FakeWindow:
    """A one-pane window on a fresh fake server, as `spg`/`spw` would hand over."""
    return FakeWindow(FakeSession(FakeServer(socket_name)))
