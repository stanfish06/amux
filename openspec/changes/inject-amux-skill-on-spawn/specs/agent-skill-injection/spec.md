## ADDED Requirements

### Requirement: Host agents get the skill installed at spawn

Spawning a host-runtime agent SHALL install amux's own skill document into the skill directory that agent reads, resolved under the agent's `$HOME`: `.claude/skills/amux/SKILL.md` for `claude` and `.codex/skills/amux/SKILL.md` for `codex`. Installation SHALL NOT depend on any prior manual step. Raw command specs, which are not amux-known agents, SHALL NOT trigger an install.

#### Scenario: Claude agent on a machine with no prior install

- **WHEN** a grid containing a `claude` agent is spawned on the host and `~/.claude/skills/amux/` does not exist
- **THEN** the directory is created and `~/.claude/skills/amux/SKILL.md` is written with amux's skill document

#### Scenario: Codex agent

- **WHEN** a grid containing a `codex` agent is spawned on the host
- **THEN** `~/.codex/skills/amux/SKILL.md` is written with amux's skill document

#### Scenario: Mixed grid installs both

- **WHEN** a grid containing both `claude` and `codex` agents is spawned on the host
- **THEN** both `~/.claude/skills/amux/SKILL.md` and `~/.codex/skills/amux/SKILL.md` are written

#### Scenario: Repeated agents of one kind write once

- **WHEN** a grid of four `claude` agents is spawned
- **THEN** the destination for `claude` is written once, not once per pane

#### Scenario: Raw command spec

- **WHEN** a grid containing only a raw command spec such as `bash` is spawned
- **THEN** no skill document is installed, because there is no agent skill directory to install into

#### Scenario: Non-repository target directory

- **WHEN** a grid is spawned in a directory that is not a git repository, so worktree isolation is skipped
- **THEN** the skill is still installed, because the destination is under `$HOME` and does not depend on the target repository

### Requirement: The install overwrites whatever occupies the destination

The install SHALL overwrite the destination unconditionally, replacing an existing file, a stale copy, or a symlink such as the one `make install_skills` creates into this repository. amux SHALL NOT write through a symlink to its target; the symlink SHALL be replaced by a regular file. The document amux writes SHALL be the copy the running amux resolves for itself, whether from a checkout or from a packaged bundle.

#### Scenario: Existing regular file is replaced

- **WHEN** a `claude` agent is spawned and `~/.claude/skills/amux/SKILL.md` already holds an older copy
- **THEN** the file is replaced with the current document

#### Scenario: Symlink is replaced, not followed

- **WHEN** a `claude` agent is spawned and `~/.claude/skills/amux/` is a symlink into an amux checkout
- **THEN** the symlink is replaced by a real directory containing a regular `SKILL.md`, and the file inside the checkout is left untouched

#### Scenario: Overwrite is reported

- **WHEN** the install replaces a symlink or a file whose contents differ from what amux writes
- **THEN** amux prints that it installed its skill at that path, so a developer can see why an edited checkout copy stopped taking effect

#### Scenario: Idempotent across spawns

- **WHEN** two grids are spawned in sequence
- **THEN** the second spawn leaves the destination with the same contents as the first, and reports nothing new when nothing changed

### Requirement: A claude agent is launched with a skill pointer in its system prompt

A `claude` agent SHALL be launched with `--append-system-prompt` carrying a short pointer that states the agent is running inside an amux pane and that amux's skill governs coordination, spawning, messaging, worktrees, and state. The pointer SHALL be short enough not to displace the document itself, SHALL name the skill so it can be invoked directly, and SHALL be appended to the agent's existing launch flags rather than replacing them. The pointer text SHALL be shell-quoted as a single argument.

#### Scenario: Host claude launch

- **WHEN** a host `claude` pane is prepared
- **THEN** its launch command is the existing `claude` command with `--append-system-prompt` and the pointer text appended as one quoted argument

#### Scenario: Sandboxed claude launch

- **WHEN** a `claude` pane is prepared under the `docker-sandbox` runtime
- **THEN** its `sbx run` attach command passes `--append-system-prompt` and the pointer through to the agent inside the sandbox

#### Scenario: Pointer does not replace the system prompt

- **WHEN** a `claude` agent is launched with the pointer
- **THEN** the agent's default system prompt is still in effect, with the pointer appended to it

#### Scenario: Raw command specs are untouched

- **WHEN** a raw command spec is prepared
- **THEN** no pointer is appended, because amux does not know that command's flags

### Requirement: A codex agent is activated by a bootstrap message

Because `codex` accepts no system-prompt append flag, a `codex` agent SHALL be activated by sending a bootstrap message into its pane after its interface is ready. The message SHALL identify amux as the sender, SHALL name the installed skill's path, and SHALL instruct the agent to read it before coordinating. It SHALL be sent following amux's own messaging discipline: the text first, then `Enter` as a separate keystroke, then a check that the input line is clear.

#### Scenario: Host codex bootstrap

- **WHEN** a host `codex` pane has been launched and its interface is ready
- **THEN** amux sends the bootstrap message, then `Enter` separately, and the agent reads the skill

#### Scenario: Sandboxed codex bootstrap

- **WHEN** a `codex` pane under the `docker-sandbox` runtime has attached and its interface is ready
- **THEN** the same bootstrap message is sent into the pane, naming the in-sandbox skill path

#### Scenario: Message is attributed

- **WHEN** the bootstrap message arrives
- **THEN** it is prefixed so the agent can tell it came from amux itself rather than from its human operator or a teammate

#### Scenario: Submission is verified

- **WHEN** the bootstrap text has been sent and `Enter` delivered
- **THEN** amux confirms the pane's input line no longer holds the text, and does not re-send the message if it was already submitted

#### Scenario: Claude does not get a bootstrap message

- **WHEN** a `claude` pane is spawned
- **THEN** no bootstrap message is sent, because its pointer is already in the system prompt and a message would consume a turn for nothing

### Requirement: A bootstrap message waits for the agent's interface

Text sent to an agent TUI that has not finished starting is silently swallowed, so amux SHALL wait for readiness before sending a bootstrap message, with a bounded timeout. Waiting SHALL NOT block the rest of the grid from coming up, and a timeout SHALL be reported rather than retried indefinitely.

#### Scenario: Interface becomes ready

- **WHEN** a `codex` agent's interface becomes ready within the timeout
- **THEN** the bootstrap message is sent once and confirmed

#### Scenario: Interface never becomes ready

- **WHEN** a `codex` agent's interface is not ready before the timeout expires
- **THEN** amux reports that the agent was not given its skill pointer, names the agent, and leaves the pane running

#### Scenario: Other panes are unaffected

- **WHEN** one agent's bootstrap wait times out
- **THEN** every other pane in the grid is spawned, launched, and activated normally

### Requirement: Injection failures degrade, they do not fail the spawn

Neither installing the document nor activating it SHALL fail a spawn. A failure SHALL be reported per affected agent, naming the agent and the reason, and SHALL leave the grid, its panes, its worktrees, and its sandboxes in place. The document is informative; the agent still functions without it.

#### Scenario: Destination is unwritable

- **WHEN** the skill destination cannot be created or written
- **THEN** amux reports the affected agent and the reason, and the grid still comes up

#### Scenario: The document is missing from this amux

- **WHEN** the running amux cannot resolve its own skill document
- **THEN** amux reports that the agent will run without it, and the grid still comes up

#### Scenario: Grid rollback is not triggered

- **WHEN** a skill install or activation step fails
- **THEN** no pane, worktree, branch, or sandbox created for that grid is unwound

#### Scenario: Installed skills are not removed on rollback

- **WHEN** a grid fails to create for an unrelated reason and is unwound
- **THEN** an already-installed skill document is left in place, because it is shared with every other grid on the machine

### Requirement: Shared-skills sandboxes are covered by the host-side install

Under `--share-skills` a sandbox's skill directory is backed by the host's, so the in-sandbox write SHALL remain skipped, and the host-side install SHALL cover that agent instead. No agent SHALL end up with neither.

#### Scenario: Sandbox grid with shared skills

- **WHEN** a `docker-sandbox` grid is spawned with `--share-skills`
- **THEN** the host-side install writes the document into the host's skill directory, which the sandbox reads, and no write is attempted inside the sandbox

#### Scenario: Sandbox grid without shared skills

- **WHEN** a `docker-sandbox` grid is spawned without `--share-skills`
- **THEN** the existing in-sandbox install delivers the document, unchanged by this capability

### Requirement: Documentation states that amux installs and overwrites the skill

The user-facing documentation SHALL state that spawning installs amux's skill, that it overwrites the destination including a `make install_skills` symlink, and what a developer editing the checkout copy must do about it.

#### Scenario: README

- **WHEN** a user reads the README
- **THEN** it states that spawning installs the skill into the agent's skill directory and overwrites what is there

#### Scenario: Makefile target

- **WHEN** a developer reads the `install_skills` target
- **THEN** it says the symlink it creates is replaced by the next spawn, and that the target must be re-run to restore a live checkout link

#### Scenario: The skill describes its own delivery

- **WHEN** an agent reads amux's skill document
- **THEN** it states that amux installed it at spawn and that this document is the authoritative vocabulary for the pane the agent is in
