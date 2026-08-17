## 1. Host-side skill install

- [x] 1.1 Add a `HostSkillInstalled` result type (path or reason) beside `SkillInstalled` in `sandbox_bootstrap.py`, mirroring its shape so both failures report identically.
- [x] 1.2 Add `install_host_skill(agent, *, home=None, source=None)` reusing `skill_source()` and `sandbox_hooks.hooks_for(agent).skills_relpath`, with `home` injectable so tests never touch a real `$HOME`.
- [x] 1.3 Make the write unconditional: create parent directories, unlink the destination path when it is a symlink so the checkout copy is never written through, then write the file at mode 644.
- [x] 1.4 Return the destination and whether the write changed anything, so the caller can report a replaced symlink or a differing file without printing on every no-op spawn.
- [x] 1.5 Unit-test against a temporary home: missing directory, existing stale file, symlinked directory, symlinked file, unwritable destination, and an unresolvable source document.
- [x] 1.6 Test that a symlinked destination's original target file is left byte-identical after the install.

## 2. Wire the install into the host runtime

- [x] 2.1 In `HostRuntime.prepare`, install the skill for each distinct agent kind in the grid, resolving each destination once rather than once per pane.
- [x] 2.2 Skip raw command specs, which have no agent skill directory.
- [x] 2.3 Report failures per affected agent, naming the agent and reason, in the same voice as the existing sandbox skill-install warning; never raise.
- [x] 2.4 Report the destination when the install replaced a symlink or a file whose contents differed, so a developer sees why an edited checkout copy stopped taking effect.
- [x] 2.5 Confirm nothing about the install participates in `GridCreationError` rollback, and add a test asserting an installed document survives an unwound grid.
- [x] 2.6 Test that a grid spawned in a non-repository directory still installs, since the destination is under `$HOME`.

## 3. Claude activation through the system prompt

- [x] 3.1 Add the pointer text as a single constant in `shared.py`: the agent is in an amux pane, amux's skill governs coordination, spawning, messaging, worktrees, and state, read it before doing any of those, and it is invocable by name.
- [x] 3.2 Append `--append-system-prompt <pointer>` to the `claude` launch command in `HostRuntime.prepare`, quoted as one argument for `send-keys` text.
- [x] 3.3 Append the same flag and pointer to `claude`'s argument list in `sandbox.attach_argv`, emitting the `<agent> -- <args>` form even though `claude` has no `AGENT_ATTACH_ARGS` entry.
- [x] 3.4 Leave raw command specs and `codex` untouched by this step.
- [x] 3.5 Test both seams: pointer present for `claude`, absent for `codex` and raw commands, existing flags preserved, and the pointer arriving as a single quoted argument.
- [x] 3.6 If `choose-agent-model-and-effort` has landed, reuse its argument render-and-quote helper rather than adding a second one. *(It had not landed; built `shared.render_command` as the single helper for both seams and published it as amux note #100 for the other change to compose with.)*

## 4. Readiness-and-send mechanism

- [x] 4.1 Add `bootstrap: str = ""` to `runtime.Launch`, documented as text sent to the agent after its interface is ready, distinct from `keys`, which are typed into a shell before the agent exists.
- [x] 4.2 Add a `core` helper that waits for an agent interface by polling `capture-pane` with a bounded timeout, keeping the match as loose as it can be while still distinguishing a ready interface from a starting one.
- [x] 4.3 Send the text with no trailing `Enter` and without libtmux's history-suppressing leading space, pause, then send `Enter` as its own keystroke.
- [x] 4.4 Verify submission by capturing the pane again; if the text is still on the input line, send `Enter` again rather than re-sending the text.
- [x] 4.5 On timeout or unconfirmed submission, report the affected agent and continue; never raise and never retry indefinitely.
- [x] 4.6 Send bootstrap payloads after every pane's launch keys have gone out, so one agent's readiness wait does not delay another pane's launch.
- [x] 4.7 Test the helper against captured pane fixtures rather than a live agent: ready interface, still-starting interface, text stuck on the input line, and timeout.

## 5. Codex activation through a bootstrap message

- [x] 5.1 Compose the codex bootstrap message from the shared pointer: an `[amux]` sender prefix so the agent can tell it is not from its human or a teammate, plus the installed skill's path.
- [x] 5.2 Set `Launch.bootstrap` for `codex` panes under the host runtime, naming the host path.
- [x] 5.3 Set `Launch.bootstrap` for `codex` panes under the `docker-sandbox` runtime, naming the in-sandbox path.
- [x] 5.4 Leave `claude` panes with an empty `bootstrap`, since their pointer is already in the system prompt.
- [x] 5.5 Test that a `codex` pane carries a bootstrap payload with the right path per runtime, and that a `claude` pane carries none.

## 6. Reverse the `--share-skills` rule

- [x] 6.1 In `DockerSandboxRuntime.prepare`, run the host-side install for the grid's agent kinds when `share_skills` is on, so that configuration is no longer left with nothing.
- [x] 6.2 Keep the in-sandbox write skipped under `share_skills`, since writing from inside the VM into the host's directory is still the wrong direction.
- [x] 6.3 Rewrite the comment at that branch: as written it argues against behaviour amux has now adopted, so it must explain the new split rather than the old refusal.
- [x] 6.4 Test both sandbox configurations: with `--share-skills` the host destination is written and no in-VM write is attempted; without it, the existing in-VM install runs unchanged.

## 7. Documentation

- [x] 7.1 Update `README.md`: spawning installs amux's skill into the agent's skill directory and overwrites what is there, including a `make install_skills` symlink.
- [x] 7.2 Update the `Makefile` comment on `install_skills` to say the symlink is replaced by the next spawn and the target must be re-run to restore a live checkout link.
- [x] 7.3 Update `skills/amux/SKILL.md` to state that amux installed it at spawn and that it is the authoritative vocabulary for the pane the agent is in.
- [x] 7.4 Correct the claim in `skills/amux/SKILL.md` that a spawned agent sits idle until prompted: a `codex` agent now receives a bootstrap message and spends a turn on it.
- [x] 7.5 Update `docs/sandbox-smoke-test.md` so its procedure checks both the installed document and the activation for each agent kind.

## 8. Verification

- [x] 8.1 Run the full test suite, including the existing sandbox skill-install and bootstrap tests, and update any that assumed the host never writes to `$HOME`.
- [x] 8.2 Spawn a host grid with one `claude` and one `codex` on a machine with `~/.claude/skills/amux` symlinked, and confirm the symlink was replaced, the checkout copy is untouched, and the replacement was reported.
- [x] 8.3 Confirm from inside the `claude` pane that it knows about amux without being asked, and from inside the `codex` pane that it received the bootstrap message and read the skill.
- [x] 8.4 Spawn a grid in a directory that is not a git repository and confirm the install still happens.
- [x] 8.5 Force each failure path — unwritable destination, unresolvable source, readiness timeout — and confirm the grid still comes up with a per-agent warning and no rollback.
