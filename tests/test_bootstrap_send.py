"""Waiting for an agent's interface, then sending it a message.

`codex` takes no system-prompt append flag, so the only way to tell it which
document governs its pane is to type into it — and text sent to a TUI that has
not finished starting is silently swallowed. There is no readiness signal to
subscribe to: a freshly spawned pane is stamped `starting` and settles to `idle`
a few seconds later precisely *because* nothing on the agent side announces "my
prompt is ready". So this is a heuristic over rendered text, and the only honest
way to test a heuristic over rendered text is against text a real agent rendered.

Every non-`synthetic_` fixture in `test_pane_readiness_fixtures/` is a real
`capture-pane` of a real `claude` 2.1.224 or `codex-cli` 0.146.0 — see the README
there for how they were taken and for the three surprises that are the reason
they are files rather than string literals in this module.

Two hazards shape the whole design, and both are measured rather than imagined:

- **A blocking chooser renders a caret too.** `codex` shows an update prompt and
  both agents show a trust-this-directory prompt *before* the composer. Typing a
  message into codex's update modal types it into a menu whose first entry runs
  `npm install -g @openai/codex`.
- **Nothing here may raise.** The helper runs inside `core._build_grid`, whose
  callers answer any exception by killing the whole session (`spw`) or the whole
  task window (`spg`). An agent that cannot be given its pointer loses its
  pointer, not its grid, and not anyone else's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fake_tmux
from amux import core, shared

FIXTURES = Path(__file__).parent / "test_pane_readiness_fixtures"

MESSAGE = shared.skill_bootstrap_message("codex", "/home/agent/.codex/skills/amux/SKILL.md")


def capture(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


#: The exact text typed into the pane when `codex_0.146.0_text_on_the_input_line`
#: and `codex_0.146.0_submitted` were captured. Asserted below to be present in
#: the first, so a re-record that changed it cannot leave these tests passing
#: while proving nothing.
STUCK = "[amux] read the amux skill at /tmp/x/SKILL.md before coordinating"


def test_the_stuck_and_submitted_fixtures_really_do_hold_that_message():
    assert STUCK in capture("codex_0.146.0_text_on_the_input_line")
    assert STUCK in capture("codex_0.146.0_submitted")


def pane(*captures: str) -> fake_tmux.FakePane:
    """A pane that answers each successive capture; the last one repeats."""
    p = fake_tmux.new_window().panes[0]
    p.captures = list(captures) or [fake_tmux.READY_CAPTURE]
    return p


class Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def send(p, text=MESSAGE, **kwargs) -> str:
    clock = kwargs.pop("clock", None) or Clock()
    return core.send_bootstrap(
        p, text, clock=clock.time, sleep=clock.sleep, **kwargs
    )


# --- reading a real interface -------------------------------------------------


READY = ["codex_0.146.0_ready", "claude_2.1.224_ready"]
NOT_READY = [
    "codex_0.146.0_starting",
    "claude_2.1.224_starting",
    "codex_0.146.0_update_modal",
    "codex_0.146.0_trust_modal",
    "claude_2.1.224_trust_modal",
    "codex_0.146.0_shell_prompt_after_exit",
    "synthetic_caret_shell_prompt",
]


@pytest.mark.parametrize("name", READY)
def test_a_composer_waiting_for_input_reads_as_ready(name):
    assert core.interface_ready(capture(name))


@pytest.mark.parametrize("name", NOT_READY)
def test_nothing_else_reads_as_ready(name):
    assert not core.interface_ready(capture(name))


def test_the_claude_composer_is_matched_through_a_non_breaking_space():
    """The trap that would have made this feature silently never work for one
    of the two agents: `claude`'s caret line is `❯` + U+00A0, not `❯` + ' '.
    An ASCII-only whitespace class never fires and the bootstrap times out
    forever, looking perfectly reasonable in review the whole time."""
    text = capture("claude_2.1.224_ready")
    assert "❯\xa0" in text
    assert core.interface_ready(text)


def test_a_caret_that_is_the_last_thing_on_screen_is_a_shell_not_a_composer():
    """`❯ ` is a common zsh prompt (starship, pure). A caret-alone matcher sends
    the bootstrap message into a shell, which then RUNS it."""
    assert not core.interface_ready("earlier output\n\n❯ ")
    # The same caret, with a status line under it, is a composer.
    assert core.interface_ready("earlier output\n\n❯ \n\n  model · /somewhere")


def test_a_blocking_chooser_is_not_ready_even_though_it_renders_a_caret():
    modal = capture("codex_0.146.0_update_modal")
    assert "›" in modal  # it looks exactly like a composer to a caret matcher
    assert "npm install -g @openai/codex" in modal  # what a stray keystroke picks
    assert not core.interface_ready(modal)


# --- the send itself ----------------------------------------------------------


def verbs(p) -> list[str]:
    return [entry[0] for entry in p.log if entry[1] == p.id]


def test_a_ready_pane_gets_the_text_then_enter_as_a_separate_keystroke():
    """Agent TUIs read a trailing `Enter` in the same `send-keys` call
    inconsistently -- it can be absorbed as a literal newline instead of
    submitting -- so the submit key goes on its own."""
    p = pane(capture("codex_0.146.0_ready"), capture("codex_0.146.0_submitted"))

    assert send(p) == ""
    assert verbs(p) == ["capture_pane", "send_keys", "enter", "capture_pane"]


def test_the_text_arrives_unmodified_and_as_literal_keys():
    p = pane(capture("codex_0.146.0_ready"), capture("codex_0.146.0_submitted"))
    send(p)

    (sent,) = [e for e in p.log if e[0] == "send_keys"]
    assert sent[2] == MESSAGE
    # `suppress_history=False`: libtmux otherwise prefixes a space to keep the
    # line out of shell history. There is no shell here, so that space would be
    # a literal first character of the message.
    # `literal=True`: without `-l`, tmux parses what it is given as key names.
    assert dict(sent[3]) == {"enter": False, "suppress_history": False, "literal": True}


def test_the_pause_between_the_text_and_enter_is_real_by_default(no_bootstrap_pause):
    """`conftest.no_bootstrap_pause` zeroes this for the suite's sake, and hands
    back what it replaced. If the default were also zero, that fixture would be
    the only reason the code looked fast and nobody would notice."""
    assert no_bootstrap_pause > 0


def test_a_starting_interface_is_waited_for_rather_than_typed_into():
    p = pane(
        capture("codex_0.146.0_starting"),
        capture("codex_0.146.0_starting"),
        capture("codex_0.146.0_ready"),
        capture("codex_0.146.0_submitted"),
    )

    assert send(p) == ""
    assert verbs(p)[:4] == ["capture_pane", "capture_pane", "capture_pane", "send_keys"]


def test_an_interface_that_never_comes_up_is_reported_and_left_running():
    clock = Clock()
    p = pane(capture("codex_0.146.0_starting"))

    problem = send(p, timeout=10.0, poll=1.0, clock=clock)

    assert "not ready" in problem and "10" in problem
    assert "send_keys" not in verbs(p)
    # Bounded, not indefinite: it stopped at the deadline rather than polling on.
    assert clock.now == pytest.approx(10.0)


def test_the_wait_never_overshoots_its_deadline():
    """A poll interval longer than what is left of the timeout must not extend
    it -- a 45s budget polled every 30s would otherwise wait 60s."""
    clock = Clock()
    send(pane(capture("codex_0.146.0_starting")), timeout=5.0, poll=30.0, clock=clock)

    assert clock.now == pytest.approx(5.0)


# --- verifying the submission -------------------------------------------------


def test_text_left_on_the_input_line_gets_enter_again_never_the_text_again():
    """Re-sending the text would submit the message twice. It is demonstrably
    already in the composer; what did not arrive is the submit key."""
    stuck = capture("codex_0.146.0_text_on_the_input_line")
    p = pane(capture("codex_0.146.0_ready"), stuck, capture("codex_0.146.0_submitted"))

    assert send(p, text=STUCK) == ""
    assert verbs(p).count("send_keys") == 1
    assert verbs(p).count("enter") == 2


def test_text_still_stuck_after_a_second_enter_is_reported():
    stuck = capture("codex_0.146.0_text_on_the_input_line")
    p = pane(capture("codex_0.146.0_ready"), stuck, stuck, stuck)

    problem = send(p, text=STUCK)

    assert "input line" in problem
    assert verbs(p).count("send_keys") == 1


def test_a_submitted_message_is_not_mistaken_for_a_stuck_one():
    """After submission the text is still on screen -- it moves into the
    transcript ABOVE the composer. Looking for it anywhere in the pane would
    read every success as a failure and press `Enter` on an empty prompt."""
    submitted = capture("codex_0.146.0_submitted")
    text = STUCK
    assert text in " ".join(submitted.split())  # still visible, in the transcript

    p = pane(capture("codex_0.146.0_ready"), submitted)

    assert send(p, text=text) == ""
    assert verbs(p).count("enter") == 1





# --- it cannot cost anyone their grid -----------------------------------------


class DeadPane:
    """A pane tmux has lost: every operation on it raises."""

    id = "%9"

    def capture_pane(self):
        raise OSError("can't find pane %9")

    def send_keys(self, *args, **kwargs):
        raise OSError("can't find pane %9")

    def enter(self):
        raise OSError("can't find pane %9")


def test_a_pane_that_died_mid_send_is_reported_not_raised():
    problem = core.send_bootstrap(DeadPane(), MESSAGE)

    assert "can't find pane" in problem


class ExplodingPane(DeadPane):
    def capture_pane(self):
        raise BaseException  # noqa: TRY002 - deliberately outside `Exception`


def test_even_an_exception_outside_the_Exception_hierarchy_is_reported():
    """`except Exception` is not enough here. `core._build_grid`'s callers catch
    `BaseException` and kill the session, so anything this helper lets through
    destroys a grid over a markdown file."""
    assert core.send_bootstrap(ExplodingPane(), MESSAGE) == "BaseException"


def test_a_keyboard_interrupt_still_reaches_the_operator():
    """The one exception to the rule: Ctrl-C means stop, and swallowing it would
    make a grid build uninterruptible."""

    class Interrupted(DeadPane):
        def capture_pane(self):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        core.send_bootstrap(Interrupted(), MESSAGE)
