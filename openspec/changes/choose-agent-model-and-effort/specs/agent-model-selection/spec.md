## ADDED Requirements

### Requirement: Agent spec grammar carries model and effort

The `-a/--agent` spec SHALL accept the grammar `AGENT[@MODEL][/EFFORT][:COUNT]`, where `@MODEL` names the model the agent runs, `/EFFORT` names its reasoning effort level, and `:COUNT` keeps its existing meaning of how many panes the spec fills. Each part SHALL be independently optional, and the spec SHALL remain valid with any subset present.

#### Scenario: Model and effort and count together

- **WHEN** a grid is spawned with `-a claude@opus/high:2`
- **THEN** two panes are filled with `claude` agents whose model is `opus` and whose effort is `high`

#### Scenario: Effort without model

- **WHEN** a grid is spawned with `-a claude/high`
- **THEN** the pane's agent is `claude` with effort `high` and no model is specified, so the agent CLI's own default model applies

#### Scenario: Model without effort

- **WHEN** a grid is spawned with `-a codex@gpt-5.6-sol`
- **THEN** the pane's agent is `codex` with model `gpt-5.6-sol` and no effort is specified, so the agent CLI's own default effort applies

#### Scenario: Neither model nor effort

- **WHEN** a grid is spawned with `-a claude:3`
- **THEN** three `claude` panes launch the exact command amux launched before this capability existed, with no model or effort flags added

#### Scenario: Count still absorbs the grid remainder

- **WHEN** a grid is spawned with `-r 2 -c 2 -a claude@opus/high:3 -a codex@gpt-5.6-sol`
- **THEN** three panes run `claude` with model `opus` and effort `high`, and the remaining one pane runs `codex` with model `gpt-5.6-sol`

### Requirement: Only known agents are parsed for model and effort

The model and effort grammar SHALL apply only to agents amux knows how to launch (`claude` and `codex`). A spec whose agent is not a known agent SHALL be treated as a raw command and passed through unchanged, including when it contains `@` or `/` characters.

#### Scenario: Raw command containing a slash

- **WHEN** a grid is spawned with `-a /usr/bin/bash`
- **THEN** the pane runs the command `/usr/bin/bash` and no part of the path is interpreted as an effort level

#### Scenario: Raw command containing an at-sign

- **WHEN** a grid is spawned with `-a 'ssh user@host'`
- **THEN** the pane runs `ssh user@host` and `host` is not interpreted as a model

#### Scenario: Raw command with a count

- **WHEN** a grid is spawned with `-a bash:2`
- **THEN** two panes run `bash`, preserving the existing count behaviour for raw commands

### Requirement: Model and effort render into each agent CLI's own flags

Model and effort SHALL be translated into the flags the target agent CLI actually accepts, per agent, rather than into one shared spelling. For `claude` the model SHALL be passed as `--model <value>` and the effort as `--effort <value>`. For `codex` the model SHALL be passed as `-m <value>` and the effort as `-c model_reasoning_effort=<value>`. Flags SHALL be appended to the agent's existing launch command, leaving its current flags in place.

#### Scenario: Claude launch command

- **WHEN** a host pane is prepared for the spec `claude@opus/high`
- **THEN** its launch command is the existing `claude --dangerously-skip-permissions` command with `--model opus --effort high` appended

#### Scenario: Codex launch command

- **WHEN** a host pane is prepared for the spec `codex@gpt-5.6-sol/xhigh`
- **THEN** its launch command is the existing `codex --dangerously-bypass-approvals-and-sandbox` command with `-m gpt-5.6-sol -c model_reasoning_effort=xhigh` appended

#### Scenario: Only the specified parts appear

- **WHEN** a host pane is prepared for the spec `claude/high`
- **THEN** the launch command carries `--effort high` and carries no `--model` flag

### Requirement: Values pass through unvalidated

amux SHALL validate the structure of a spec but SHALL NOT validate model or effort values against any list it maintains. A model name or effort level unknown to amux SHALL be forwarded to the agent CLI, which owns the decision to accept or reject it. A structurally malformed spec SHALL be rejected before any pane, worktree, or sandbox is created.

#### Scenario: Unknown effort level is forwarded

- **WHEN** a grid is spawned with `-a claude/some-new-level`
- **THEN** amux launches `claude` with `--effort some-new-level` rather than refusing the spec

#### Scenario: Unknown model is forwarded

- **WHEN** a grid is spawned with `-a claude@some-new-model`
- **THEN** amux launches `claude` with `--model some-new-model` rather than refusing the spec

#### Scenario: Empty model is a structural error

- **WHEN** a grid is spawned with `-a claude@`
- **THEN** amux reports that the spec is malformed, exits non-zero, and creates no session, window, pane, worktree, or sandbox

#### Scenario: Empty effort is a structural error

- **WHEN** a grid is spawned with `-a claude@opus/`
- **THEN** amux reports that the spec is malformed, exits non-zero, and creates no session, window, pane, worktree, or sandbox

#### Scenario: Values are shell-quoted

- **WHEN** a model or effort value contains shell metacharacters
- **THEN** the value reaches the agent CLI as a single argument, and the metacharacters are not interpreted by the shell that starts the agent

### Requirement: Both runtimes honour model and effort

The host runtime and the `docker-sandbox` runtime SHALL both apply a spec's model and effort. Under `docker-sandbox` the flags SHALL be appended to the agent's argument list in the sandbox attach command, composing with any flags that runtime already requires for that agent. Neither runtime SHALL silently ignore a model or effort a user asked for.

#### Scenario: Sandboxed claude carries the flags

- **WHEN** a `claude@opus/high` pane is prepared under the `docker-sandbox` runtime
- **THEN** its attach command passes `--model opus --effort high` through to the agent inside the sandbox

#### Scenario: Sandboxed codex composes with the hook-trust flag

- **WHEN** a `codex@gpt-5.6-sol/xhigh` pane is prepared under the `docker-sandbox` runtime
- **THEN** its attach command carries both the existing hook-trust flag and `-m gpt-5.6-sol -c model_reasoning_effort=xhigh`

#### Scenario: Runtimes agree

- **WHEN** the same spec is spawned once under the host runtime and once under `docker-sandbox`
- **THEN** both agents run the same model and effort

### Requirement: Recorded agent identity stays the bare agent kind

The agent kind recorded for a pane SHALL remain the bare agent name (`claude` or `codex`) wherever amux already records it, including the pane's agent option, the context store's agent column, hook installation, sandbox agent-support checks, and state reporting. Model and effort SHALL NOT be folded into that value.

#### Scenario: Pane option is unchanged

- **WHEN** a pane is created from the spec `claude@opus/high`
- **THEN** its recorded agent kind is `claude`, so state events, `is_agent` checks, and monitoring behave exactly as for a plain `claude` pane

#### Scenario: Sandbox agent support check is unaffected

- **WHEN** a sandbox is created for the spec `codex@gpt-5.6-sol/xhigh`
- **THEN** the supported-agent check sees `codex` and passes

### Requirement: An agent can discover its own model and effort

`amux ctx` SHALL report the model and effort a pane was launched with, so an agent and its teammates can read a pane's configuration after spawn rather than only from the spawning shell's history. A pane launched without a model or effort SHALL NOT report a value it did not receive.

#### Scenario: Agent reads its own configuration

- **WHEN** an agent spawned from `claude@opus/high` runs `amux ctx`
- **THEN** the output identifies its model as `opus` and its effort as `high`

#### Scenario: Machine-readable form

- **WHEN** an agent spawned from `claude@opus/high` runs `amux ctx --json`
- **THEN** its `self` object carries the model and effort as separate fields

#### Scenario: Defaults are not invented

- **WHEN** an agent spawned from a plain `claude` spec runs `amux ctx`
- **THEN** no model or effort is reported for it, because amux did not choose either one

### Requirement: Documented grammar matches the implemented grammar

The user-facing help text and the documentation that agents read as authoritative SHALL describe the `AGENT[@MODEL][/EFFORT][:COUNT]` grammar, including the raw-command escape hatch for model ids that collide with the delimiters.

#### Scenario: CLI help

- **WHEN** a user runs `amux spw --help`
- **THEN** the `-a/--agent` help text shows the full grammar with model and effort

#### Scenario: Agent-facing skill documentation

- **WHEN** an agent reads the bundled amux skill documentation
- **THEN** it finds the model and effort grammar, an example spec using it, and the note that a model id containing `/` or ending in `:<digits>` must be launched as a raw command
