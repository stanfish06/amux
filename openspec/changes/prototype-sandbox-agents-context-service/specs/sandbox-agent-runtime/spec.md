## ADDED Requirements

### Requirement: Sandbox execution is explicit and optional
Amux SHALL retain `host` as the default agent runtime and SHALL select Docker Sandboxes only when `spw` or `spg` is invoked with `--runtime docker-sandbox`. A grid using the Docker Sandbox runtime SHALL accept the existing `claude` and `codex` agent kinds and MUST reject unsupported raw agent commands before creating panes or sandboxes.

#### Scenario: Existing command keeps host behavior
- **WHEN** a user spawns a workspace or task without a runtime flag
- **THEN** amux launches the agents with the existing host runtime and creates the existing per-agent host worktrees

#### Scenario: User selects sandbox execution
- **WHEN** a user spawns a workspace or task with `--runtime docker-sandbox` and supported agents
- **THEN** amux launches one Docker Sandbox-backed agent per pane without changing the default for other workspaces or tasks

#### Scenario: Unsupported agent is rejected
- **WHEN** a Docker Sandbox grid includes an agent kind other than the supported Claude or Codex kinds
- **THEN** amux exits with an actionable error before creating any tmux pane, sandbox, branch, or registry row

### Requirement: Sandbox prerequisites are checked before mutation
Amux MUST verify that `sbx` is installed, the requested resource values are valid, the target is a Git repository with a usable primary checkout, the context service is healthy, and the sandbox network policy can reach the configured context endpoint before creating a sandbox grid. Amux MUST report authentication or policy failures without changing Docker's global policy automatically.

#### Scenario: Preflight succeeds
- **WHEN** all Docker Sandbox, repository, resource, context-service, and network checks pass
- **THEN** amux proceeds to create the task integration branch, agent records, sandboxes, and panes

#### Scenario: Preflight fails
- **WHEN** any required executable, authentication state, repository condition, context-service health check, resource value, or network rule is unavailable
- **THEN** amux exits non-zero with the failed check and remediation command and leaves no new tmux session, sandbox, Git reference, or active registry record

### Requirement: Each sandbox agent has an isolated and reproducible execution identity
For a Docker Sandbox grid, amux SHALL create one uniquely named clone-mode sandbox per agent from the repository's primary checkout. Amux SHALL assign a deterministic amux branch name, apply explicit CPU and memory limits, disable Docker's shared read-write skills store unless the user explicitly opts in, bootstrap the sandbox context client, and record the runtime, sandbox name, sandbox identifier, pane, agent, repository, branch, and lifecycle status in the host context store.

#### Scenario: Sandboxed agent is created
- **WHEN** amux creates a Docker Sandbox agent successfully
- **THEN** the sandbox has a private clone, an `amux/<workspace>/<task>/<agent-name>` branch, configured resource limits, a working context client, and an active host registry record bound to its tmux pane

#### Scenario: Shared skills are not requested
- **WHEN** a user selects Docker Sandbox execution without the explicit shared-skills opt-in
- **THEN** amux creates every sandbox with Docker's shared skills store disabled

#### Scenario: Partial grid creation fails
- **WHEN** one sandbox fails after another sandbox in the same new grid was created
- **THEN** amux removes or marks failed all resources created for that grid and reports every cleanup failure without hiding the original error

### Requirement: Sandboxed work is integrated through committed Git branches
Sandboxed agents SHALL perform Git operations inside their private clones. Before integrating a sandboxed agent, amux MUST fetch the agent's named branch from Docker's host-side sandbox remote and SHALL merge that fetched commit into the existing task integration branch using the same non-fast-forward and conflict-reporting semantics as host worktrees. Amux MUST NOT treat uncommitted sandbox files as integrated work.

#### Scenario: Sandbox branch integrates successfully
- **WHEN** a sandboxed agent has committed changes on its assigned branch and the user runs `amux integrate`
- **THEN** amux fetches the sandbox branch, merges it into the task integration branch, records the commit and short-stat result, and marks the agent record merged

#### Scenario: Sandbox branch conflicts
- **WHEN** the fetched sandbox branch conflicts with the task integration branch
- **THEN** amux aborts the merge, leaves the sandbox and its commits intact, records a task-scoped blocker note, and returns a non-zero result for that agent

#### Scenario: Sandbox has no committed work
- **WHEN** the sandbox branch has no commits beyond the task base or contains only uncommitted changes
- **THEN** amux does not report those files as integrated and explains whether the agent must commit or there is no branch delta

### Requirement: Sandbox lifecycle follows amux lifecycle without silent data loss
Killing a sandbox-backed task or workspace without `--clean` SHALL stop its sandboxes while preserving their VM state and committed branches. Cleaning SHALL preserve committed branch tips on the host before removing sandboxes and revoking context credentials. If a sandbox contains uncommitted changes, cleanup MUST refuse unless the user supplies an explicit force option that describes the data loss.

#### Scenario: Task is stopped without cleanup
- **WHEN** the user kills a sandbox-backed task without `--clean`
- **THEN** amux stops each sandbox, retains its runtime metadata and contents, and permits a later reattachment

#### Scenario: Clean preserves committed work
- **WHEN** the user cleans a sandbox-backed task whose sandboxes have clean working trees
- **THEN** amux fetches and preserves each committed branch tip, removes the Docker sandboxes and their host remotes, revokes their context credentials, and marks their registry records removed

#### Scenario: Clean encounters uncommitted work
- **WHEN** a sandbox working tree is dirty and the user did not explicitly authorize forced cleanup
- **THEN** amux refuses to remove that sandbox and reports the files or status that must be resolved

### Requirement: Runtime state is visible in existing amux views
Amux SHALL include runtime kind and sandbox identity in machine-readable context and listing output for sandbox-backed agents while preserving existing fields for host agents. The monitor SHALL continue to resolve starting, busy, idle, needs-input, stopped, and dead states from host tmux facts plus context-service events.

#### Scenario: User inspects a mixed installation
- **WHEN** host-backed and sandbox-backed workspaces exist simultaneously
- **THEN** `amux ctx --json`, list commands, and the monitor distinguish their runtimes and show the sandbox name and lifecycle state only where applicable

### Requirement: Prototype behavior is verifiable without a live provider session
The sandbox runtime adapter SHALL support deterministic unit and integration tests using a fake `sbx` executable and isolated Git repositories. A separately documented manual smoke test SHALL measure spawn-to-prompt time, four-agent CPU and memory use, disk growth, network-policy failures, branch integration, and cleanup on a supported Docker Sandbox host.

#### Scenario: Automated sandbox tests run offline
- **WHEN** the test suite runs without Docker authentication or model-provider credentials
- **THEN** fake-CLI tests cover command construction, preflight failure, rollback, branch fetch and merge, lifecycle transitions, and token revocation without launching a real sandbox

