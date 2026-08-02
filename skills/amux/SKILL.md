---
name: amux
description: Use when orchestrating AI agents in tmux with the amux CLI — spawning or killing agent grids (`amux spw`/`spg`/`kw`/`kg`), listing them (`lsw`/`lsg`), mixed claude+codex grids, the `amux-root` tmux socket, agent state events (`amux event emit`/`tail`/`wait`), sending messages from one agent pane to another, or when an agent needs to discover which workspace, task, and pane it is running in (`amux ctx`).
---

# amux

Agent orchestration on top of tmux. One dedicated tmux server (socket `amux-root`)
holds every agent, so amux never touches your interactive tmux sessions.

## Vocabulary

amux renames tmux concepts. Its output uses the right-hand column; raw `tmux`
commands use the left.

| tmux    | amux        | Meaning                        |
|---------|-------------|--------------------------------|
| session | workspace   | one project / repo             |
| window  | task        | one unit of work               |
| pane    | agent       | one running `claude` / `codex` |

## Quick reference

| Command | Does |
|---|---|
| `amux spw <ws> [-p DIR] [-t TASK] [-a SPEC] [-r N] [-c N]` | spawn workspace + its first task grid |
| `amux spg <ws> <task> [-p DIR] [-a SPEC] [-r N] [-c N]` | add a task grid to an existing workspace |
| `amux lsw` | list all workspaces, tasks, panes |
| `amux lsg <ws>` | list task grids in one workspace |
| `amux monitor [-W COLS] [-T COLS] [-i MS]` | live read-only dashboard of every workspace and agent |
| `amux kg <ws> <task>` | kill one task |
| `amux kw <ws>` | kill a whole workspace |
| `amux ctx [--json] [--pane ID]` | this agent's identity + workspace team roster |
| `amux event tail [-n N] [--pane ID]` | recent state events as JSONL |
| `amux event wait <pane> [--timeout S]` | block until a pane goes idle / needs-input / dead |
| `amux event emit <kind> [--pane ID] [--agent K] [--detail T]` | record a state change (for hooks) |

`-p` defaults to `$PWD` for `spw`, to the workspace dir for `spg`. `-t` defaults
to `task0`. Global `-L/--socket-name` must come **before** the subcommand.

## Agent specs and grid shape

`-a AGENT[:COUNT]` is repeatable and fills panes row-major. `AGENT` is `claude`,
`codex`, or any raw shell command. Default is one `claude`.

```sh
amux spw myproj -p ~/Git/myproj -r 2 -c 2   # 2x2, all claude
amux spg myproj review -a codex:2           # 1x2, both codex
amux spg myproj fix -a claude:3 -a codex    # 2x2, 3 claude + 1 codex
amux spg myproj shell -a bash               # raw command instead of an agent
```

Shape resolution, given `n` total agents:

- both `-r` and `-c`: must satisfy `r*c == n`, else error.
- one of them: the other is `n / given`; must divide evenly, else error.
- neither: the factor pair closest to square, rows ≤ cols (so 6 → 2x3).

Counts and shape interact: **when both `-r` and `-c` are given, exactly one spec
may omit its count** and it absorbs the remaining panes (`-r 2 -c 2 -a claude:3
-a codex` → the lone `codex` fills 1). With unknown or partial shape, a missing
count means 1.

## Agent identity

Every pane gets a stable `adjective-noun` name (e.g. `brave-hawk`), a grid label
`r<row>c<col>`, and tmux pane options `@amux_agent` / `@amux_label` /
`@amux_name` / `@amux_state`. Pane titles are locked (`allow-set-title off`) so
apps can't overwrite the name.

An agent discovers itself and its teammates with no arguments:

```sh
amux ctx           # human-readable: you: brave-hawk claude @r0c1 %7 task:fix ...
amux ctx --json    # {"self": {...}, "team": [{"task": ..., "agents": [...]}]}
```

Addresses in `ctx` output — `@r0c1` (label) and `%7` (tmux pane id) — are what
you use to target a specific teammate pane.

## State events

`amux event emit <kind>` appends to `~/.local/state/amux/events.jsonl`, sets the
pane's `@amux_state`, and signals a tmux `wait-for` channel. Kinds map to states:

| kind | state |
|---|---|
| `spawn` | starting |
| `busy` | busy |
| `stop` | idle |
| `notify` | needs-input |
| `exit` | dead |

Wire it from agent hooks — Claude Code's `PreToolUse` → `busy`, `Stop` → `stop`,
`Notification` → `notify`, `SessionEnd` → `exit`. With `--detail` omitted, the
detail is pulled from the hook's JSON on stdin (`message` / `tool_name` /
`reason`). `emit` swallows all errors so a hook never looks like agent failure.

To coordinate, block on a teammate instead of polling:

```sh
amux event wait %7 --timeout 120   # prints the state; exit 0 ok, 1 timeout, 2 dead
```

## Messaging teammates

There is no `amux send`/`read` yet — it's a project goal. Today you push text
into a teammate's pane with raw tmux:

```sh
tmux -L amux-root send-keys -t %9 'your message' Enter
```

**Always lead with your own identity.** `send-keys` types raw keystrokes into
the target agent's prompt — there is no envelope, no sender field, nothing that
distinguishes your message from the receiver's own human operator or from a
third teammate. An unattributed message leaves the receiver unable to reply or
to weigh who is asking. Take your name, label, and pane id from `amux ctx` and
prefix every message:

```sh
# from brave-hawk (@r0c1 %7) asking golden-owl (%9) for a review
tmux -L amux-root send-keys -t %9 \
  '[amux brave-hawk @r0c1 %7] auth tests pass; please review src/auth and reply to %7' Enter
```

Include the pane id, not just the name — it's what the other agent needs to
address you back. When you expect a reply, say so explicitly and name your pane;
the receiver has no return channel otherwise.

### Send the text, then Enter separately

A message with no submit key just sits in the receiver's input box. Nothing
errors, the sender sees success, and the other agent never wakes up. Agent TUIs
also read a trailing `Enter` in the *same* `send-keys` call inconsistently — it
can be absorbed into the input as a literal newline instead of submitting. Send
the text, pause, then send `Enter` on its own:

```sh
tmux -L amux-root send-keys -t %9 '[amux brave-hawk @r0c1 %7] please review src/auth and reply to %7'
sleep 0.3
tmux -L amux-root send-keys -t %9 Enter
```

Then confirm it actually went through — the receiver's state should leave `idle`
(`amux ctx`), or look at the pane and check the box is empty:

```sh
tmux -L amux-root capture-pane -p -t %9 | tail -5
```

If your text is still visible on the input line, it was never submitted; send
`Enter` again rather than re-sending the message (that would duplicate it).

Before sending, check the target's state in `amux ctx` (or block on
`amux event wait %9`) — keystrokes sent to a `busy` agent land mid-run and may
be swallowed by whatever prompt is active.

## Attaching

```sh
tmux -L amux-root attach -t myproj      # attach to a workspace
tmux -L amux-root ls                    # raw view of the amux server
```

## Common mistakes

- **`amux -L foo spw` vs `amux spw -L foo`** — the socket flag is global and
  must precede the subcommand.
- **Inventing a messaging command.** There is no `amux send`/`read` yet — see
  *Messaging teammates* for the `send-keys` workaround.
- **Sending an unsigned message.** A bare `send-keys` payload arrives with no
  sender. Prefix `[amux <name> @<label> <pane>]` so the receiver knows who wrote
  it and where to reply.
- **Forgetting to submit.** Text sent without a separate `Enter` sits in the
  receiver's input box forever and silently succeeds from your side. Send the
  text, pause, send `Enter`, then verify the input line is clear.
- **Expecting events outside amux.** `emit` and `wait` are no-ops unless `$TMUX`
  points at the `amux-root` socket. `ctx` without `--pane` needs `$TMUX_PANE`;
  from outside, pass `--pane %7` explicitly.
- **Reusing a workspace name.** `spw` errors if the session exists — use `spg`
  to add a task to it, or `kw` first.
- **Counting panes wrong.** `-a claude:3` in a `-r 2 -c 2` grid leaves one pane;
  a second countless spec is an error, not a fill.
- **Using plain `tmux`.** Without `-L amux-root` you're looking at a different
  server and won't see any agents.
