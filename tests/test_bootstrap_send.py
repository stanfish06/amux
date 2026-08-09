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
  both agents show a trust-this-directory prompt *before* the composer — and
  codex raises a model-switch chooser mid-session, unprompted. Typing a message
  into codex's update modal types it into a menu whose first entry runs
  `npm install -g @openai/codex`. In a small pane a chooser's later options fall
  below the fold, so only its *first* option is on screen to recognise it by.
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
    """A clock that only moves when something sleeps on it.

    Given a pane, it records each sleep INTO THAT PANE'S LOG rather than into a
    list of its own. The difference matters: a separate list can only say that
    sleeping happened, while one shared timeline says *where* -- and the whole
    point of the pause is that it falls between the text and its `Enter`.
    """

    def __init__(self, pane=None) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self._pane = pane

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self._pane is not None:
            self._pane.log.append(("sleep", self._pane.id, seconds))
        self.now += seconds


def send(p, text=MESSAGE, **kwargs) -> str:
    clock = kwargs.pop("clock", None) or Clock(pane=p)
    return core.send_bootstrap(
        p, text, clock=clock.time, sleep=clock.sleep, **kwargs
    )


# --- reading a real interface -------------------------------------------------


READY = [
    "codex_0.146.0_ready",
    "claude_2.1.224_ready",
    "codex_0.146.0_80x8_real_message_submitted",
]
NOT_READY = [
    "codex_0.146.0_starting",
    "claude_2.1.224_starting",
    "codex_0.146.0_update_modal",
    "codex_0.146.0_trust_modal",
    "codex_0.146.0_80x8_trust_modal_truncated",
    "codex_0.146.0_80x8_model_chooser",
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
    submitting -- so the submit key goes on its own.

    The `sleep` entries are the point, not noise. Asserting only the order of
    the keystrokes leaves the pause itself unpinned: deleting it passes every
    other test in this file and produces a real-world intermittent -- an `Enter`
    that arrives too soon to be read as a submit -- that no test would ever see.
    Whether the pause is nonzero is a separate question, pinned below; this pins
    that there IS one, and where.
    """
    p = pane(capture("codex_0.146.0_ready"), capture("codex_0.146.0_submitted"))

    assert send(p) == ""
    assert verbs(p) == [
        "capture_pane", "send_keys", "sleep", "enter", "sleep", "capture_pane"
    ]


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
    # It sleeps BETWEEN captures rather than spinning on `capture-pane`.
    assert verbs(p)[:6] == [
        "capture_pane", "sleep", "capture_pane", "sleep", "capture_pane", "send_keys"
    ]


def test_an_interface_that_never_comes_up_is_reported_and_left_running():
    clock = Clock()
    p = pane(capture("codex_0.146.0_starting"))

    problem = send(p, timeout=10.0, poll=1.0, clock=clock)

    assert "not ready" in problem and "10" in problem
    assert "send_keys" not in verbs(p)
    # Bounded, not indefinite: it stopped at the deadline rather than polling on.
    assert clock.now == pytest.approx(10.0)


def test_the_reason_and_the_readiness_decision_read_the_same_region():
    """Two callers of one scan, so they cannot disagree about what is on screen.

    They could before: readiness scanned from the last caret down while the
    timeout reason scanned the whole pane, so a pane whose TRANSCRIPT held a
    numbered list was correctly judged chooser-free and then told the operator
    it was "most likely asking whether to trust this directory". Wrong
    explanation rather than wrong action, and unreachable at spawn -- but the
    reason for bounding the scan was that this helper outlives the spawn.
    """
    starting = "1. Install the dependencies\n2. Run them\n\n" + capture(
        "codex_0.146.0_starting"
    )
    assert not core.interface_ready(starting)

    problem = send(pane(starting), timeout=1.0)

    assert "not ready" in problem
    assert "trust" not in problem


def test_a_pane_parked_on_a_prompt_says_so_rather_than_just_timing_out():
    """Measured on a live spawn, and the reason this reason exists: a real
    `codex` timed out at 45s because it was asking whether to trust the
    directory -- and every amux agent gets a FRESH worktree, so this is the
    ordinary case. "Not ready" alone sends the operator hunting a bug in amux
    instead of looking at the prompt sitting in the pane."""
    problem = send(pane(capture("codex_0.146.0_trust_modal")), timeout=1.0)

    assert "waiting on a prompt of its own" in problem
    assert "trust" in problem


def test_a_pane_that_simply_has_not_painted_yet_says_that_instead():
    problem = send(pane(capture("codex_0.146.0_starting")), timeout=1.0)

    assert "not ready" in problem
    assert "trust" not in problem


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
    # The retry `Enter` gets its own pause too -- it is the same keystroke with
    # the same reason to be read too early.
    assert verbs(p) == [
        "capture_pane", "send_keys", "sleep", "enter", "sleep", "capture_pane",
        "enter", "sleep", "capture_pane",
    ]


def test_text_still_stuck_after_a_second_enter_is_reported():
    stuck = capture("codex_0.146.0_text_on_the_input_line")
    p = pane(capture("codex_0.146.0_ready"), stuck, stuck, stuck)

    problem = send(p, text=STUCK)

    assert "input line" in problem
    assert verbs(p).count("send_keys") == 1


def test_shared_submission_reports_a_message_left_in_the_composer():
    stuck = capture("codex_0.146.0_text_on_the_input_line")
    p = pane(capture("codex_0.146.0_ready"), stuck)
    clock = Clock(pane=p)

    problem = core.submit_to_interface(
        p,
        STUCK,
        timeout=1.0,
        poll=0.1,
        pause=0.0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    assert "input line" in problem


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





# --- the REAL payload, which is 491 characters and wraps ----------------------
#
# Everything above uses a 65-character stand-in that fits on one composer line,
# so none of it exercises what `_PROBE_CHARS` exists for. These four use the
# actual message, captured sitting in a real composer at two pane sizes.


REAL_STUCK_WIDE = "codex_0.146.0_120x30_real_message_stuck"
REAL_SUBMITTED = "codex_0.146.0_120x30_real_message_submitted"
REAL_STUCK_TINY = "codex_0.146.0_80x8_real_message_stuck"


def test_the_real_message_wraps_across_the_composer():
    """The premise the rest of this section rests on, asserted rather than
    assumed: a stand-in that fits on one line proves nothing about wrapping."""
    assert len(MESSAGE) > 400
    composer = capture(REAL_STUCK_WIDE).splitlines()
    carets = [i for i, line in enumerate(composer) if core._CARET.match(line)]
    assert len(composer[carets[-1] :]) > 4  # the message occupies several lines


def test_a_wrapped_stuck_message_is_recognised_as_stuck():
    p = pane(capture("codex_0.146.0_ready"), capture(REAL_STUCK_WIDE), capture(REAL_SUBMITTED))

    assert send(p) == ""
    assert verbs(p).count("enter") == 2  # it retried, so it saw the message
    assert verbs(p).count("send_keys") == 1


def test_a_wrapped_submitted_message_is_recognised_as_submitted():
    p = pane(capture("codex_0.146.0_ready"), capture(REAL_SUBMITTED))

    assert send(p) == ""
    assert verbs(p).count("enter") == 1


def test_a_message_whose_head_scrolled_out_of_the_composer_is_still_stuck():
    """The measurement that made the probe read from the END of the message.

    An 80x8 pane is an ordinary quarter of a 2x2 grid, and the 491-character
    message does not fit in its composer -- the top scrolls away and only the
    tail is on screen. A probe taken from the HEAD of the message finds nothing
    there, at ANY length, so a message plainly sitting unsubmitted reads as
    submitted: no retry `Enter`, no warning, and the text sits in the box.
    """
    stuck = capture(REAL_STUCK_TINY)
    assert MESSAGE[:40] not in " ".join(stuck.split())  # the head really is gone
    assert MESSAGE[-40:] in " ".join(stuck.split())

    p = pane(capture("codex_0.146.0_ready"), stuck, capture(REAL_SUBMITTED))

    assert send(p) == ""
    assert verbs(p).count("enter") == 2


def test_a_submitted_message_in_a_small_pane_is_not_mistaken_for_a_stuck_one():
    """The other direction at 80x8, which the stuck-only pair left unpinned.

    A tail probe is the right call precisely because the tail is what a small
    composer keeps -- so it is worth checking that a pane which really did
    submit does not still show it, or every success there would retry `Enter`
    onto an empty prompt.
    """
    p = pane(capture("codex_0.146.0_ready"), capture("codex_0.146.0_80x8_real_message_submitted"))

    assert send(p) == ""
    assert verbs(p).count("enter") == 1


def test_a_chooser_whose_second_option_is_below_the_fold_is_still_a_chooser():
    """Found by taking the capture above, and it was a live defect.

    In an 80x8 pane the trust modal's `2. No, quit` and `Press enter to
    continue` fall BELOW THE VISIBLE AREA. A matcher needing a second option
    sees a lone `> 1. Yes, continue`, calls the modal a composer, and the
    `Enter` after the message lands on the highlighted first option -- amux
    silently answering a trust prompt for the user, which is the one thing this
    whole feature is not allowed to do.
    """
    truncated = capture("codex_0.146.0_80x8_trust_modal_truncated")
    assert "2." not in truncated  # the giveaway really is off screen
    assert "1. Yes, continue" in truncated

    assert not core.interface_ready(truncated)


def test_a_numbered_list_in_the_transcript_does_not_make_a_pane_unreachable():
    """`1.` in an agent's own output is not a menu.

    A chooser's caret marks its selected option and the menu runs beneath it; a
    numbered list an agent printed is always above the composer. Scanning the
    whole pane conflates them, which costs nothing at spawn -- the transcript is
    empty then -- and silently breaks the `amux send` this helper exists to be
    reused by, since by that point a pane has said plenty.
    """
    ready = capture("codex_0.146.0_ready")
    assert core.interface_ready(ready)

    assert core.interface_ready("1. Install the dependencies\n2. Run them\n\n" + ready)


def test_a_chooser_is_caught_by_the_option_on_its_own_caret_line():
    """What actually catches every real chooser: the caret marks the SELECTED
    option, so the caret line itself is numbered.

    An earlier version of this test was called
    `..._still_reaches_options_under_the_caret` and asserted the same thing,
    which was a lie by name -- narrowing the scan to the caret line alone passes
    it. That clause of the implementation is deliberate breadth for a chooser
    shape no capture has (an unnumbered caret over a numbered menu) and NOTHING
    PINS IT. Said here rather than dressed up, because a test whose name
    overclaims is worse than an absent one.
    """
    assert not core.interface_ready(
        "some output\n\n› 1. Yes, continue\n  2. No, quit\n\n  Press enter to continue"
    )
    # The caret line alone is enough, in every real capture and here.
    assert not core.interface_ready("some output\n\n› 2. No, quit\n\n  a footer")


def test_a_chooser_raised_mid_session_is_caught_too():
    """Choosers are not only a startup phenomenon: codex offered to switch model
    on its own, several turns in, while this fixture was being captured."""
    assert not core.interface_ready(capture("codex_0.146.0_80x8_model_chooser"))


def test_the_probe_is_short_enough_to_survive_a_truncated_composer(monkeypatch):
    """The upper cliff, measured against the 80x8 capture: 189 characters still
    matches, 190 does not, and past it a stuck message reads as sent.

    `monkeypatch` rather than save-and-restore on purpose. Restoring a literal
    40 here would put the value back whatever the module said, so a mutated
    default would be masked for every test after this one -- the test would
    hold the code correct by overwriting it.
    """
    monkeypatch.setattr(core, "_PROBE_CHARS", 200)
    assert not core._held_in_the_composer(capture(REAL_STUCK_TINY), MESSAGE)
    monkeypatch.undo()
    assert core._held_in_the_composer(capture(REAL_STUCK_TINY), MESSAGE)


def test_the_probe_is_long_enough_to_identify_the_message(monkeypatch):
    """The lower cliff, from the same captures, and it is a narrow one: only a
    1-character probe false-matches the footer of a pane that submitted cleanly
    -- 2 is already clean. That costs a live agent a spurious extra `Enter` on
    an empty prompt."""
    monkeypatch.setattr(core, "_PROBE_CHARS", 1)
    assert core._held_in_the_composer(capture(REAL_SUBMITTED), MESSAGE)
    monkeypatch.undo()
    assert not core._held_in_the_composer(capture(REAL_SUBMITTED), MESSAGE)


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

