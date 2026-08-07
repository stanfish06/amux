# Captured agent panes

Every `*.txt` here except `synthetic_*` is a real `tmux capture-pane -p` of a
real agent, taken on a 120x30 pane on a throwaway tmux socket. They exist
because readiness detection is a heuristic over a rendered TUI, and a heuristic
tested against invented text tests the inventor's idea of the TUI.

How they were taken:

```sh
tmux -L cap-probe new-session -d -s probe -c <empty dir> -x 120 -y 30
tmux -L cap-probe send-keys -t probe '<the launch command>'
tmux -L cap-probe send-keys -t probe Enter
tmux -L cap-probe capture-pane -p -t probe > <fixture>.txt
```

`claude` 2.1.224 and `codex-cli` 0.146.0, the versions in the filenames.

Three of these were surprises, and each one is why the file is here rather than
a string literal in a test:

- **`claude_2.1.224_ready.txt`** ends its composer line with a NON-BREAKING
  SPACE (`❯\xa0`, U+00A0), not a space. A matcher written against `[ \t]` never
  fires for `claude`, and the symptom is not a crash — it is a bootstrap that
  times out forever while looking perfectly reasonable in review.
- **`claude_2.1.224_ready.txt`** still shows the shell line that launched it.
  `claude` does not repaint over its scrollback, so "the launch command is no
  longer visible" is *not* a usable signal for the interface having come up.
  `codex_0.146.0_ready.txt` does clear it — the two agents differ.
- **`codex_0.146.0_update_modal.txt`**, **`codex_0.146.0_trust_modal.txt`** and
  **`claude_2.1.224_trust_modal.txt`** are blocking choosers that appear
  *before* the composer, own the keyboard, and render a caret exactly like a
  composer does. Typing a bootstrap message into the update modal would be
  typing it into a menu whose first entry runs
  `npm install -g @openai/codex`. Distinguishing these is not politeness.

  Both agents ask about trusting the directory, and amux spawns every agent into
  a *fresh worktree* — so this is the ordinary case on a real spawn, not an edge
  one. An agent parked on a trust prompt never becomes ready, its bootstrap send
  times out and says so, and the pane is left exactly as it was for its human.

## The three `real_message` captures

`codex_0.146.0_text_on_the_input_line.txt` and `..._submitted.txt` were taken
with a 65-character stand-in that fits on one composer line. The actual bootstrap
message is **491 characters** and wraps across five or more lines, so those two
exercise none of the wrapping the submission check has to survive. These three
hold the real payload, typed into a real composer:

- `codex_0.146.0_120x30_real_message_stuck.txt` — wrapped, whole message visible.
- `codex_0.146.0_120x30_real_message_submitted.txt` — the same message after
  `Enter`. Note it is *still on screen*, in the transcript above the composer:
  looking for the text anywhere in the pane reads every success as a failure.
- `codex_0.146.0_80x8_real_message_stuck.txt` — the same message in a pane the
  size of one quarter of a 2x2 grid. **The composer scrolls its own head away**,
  leaving only the tail. This is the capture that decided `_probe` reads from the
  END of the message: a head-based probe finds nothing here at any length, so a
  message plainly sitting unsubmitted reads as submitted, the retry `Enter` never
  fires, and amux reports no problem.

Together they pin both cliffs behaviourally rather than by asserting a constant:
a probe of ~1 character matches the footer of a cleanly submitted pane, and one
of ~110 or more no longer fits in what the 80x8 capture shows.

`codex_0.146.0_shell_prompt_after_exit.txt` is this developer's shell prompt,
kept because it is the shape a capture takes when the agent is not running at
all. `synthetic_caret_shell_prompt.txt` is the only invented file, marked as
such per the suite's convention: it is a minimal shell prompt whose prompt
character is `❯`, which is common enough (starship, pure) that a caret-alone
matcher would send a bootstrap message straight into a shell.
