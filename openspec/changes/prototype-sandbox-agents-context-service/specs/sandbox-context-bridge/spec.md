## ADDED Requirements

### Requirement: The amux context database remains host-only
The sandbox context service SHALL run on the host and SHALL be the only interface through which sandboxed processes access amux context. Amux MUST NOT mount, copy, synchronize, or otherwise expose `context.db`, its WAL or shared-memory files, the amux state directory, or the host tmux socket to a sandbox.

#### Scenario: Sandbox requests context
- **WHEN** a sandboxed agent requests its identity, roster, notes, or state
- **THEN** the host service reads the authoritative SQLite store and returns a scoped response without exposing a filesystem path or database handle

#### Scenario: Sandbox configuration is inspected
- **WHEN** the sandbox's mounts and environment are inspected
- **THEN** no mount or path grants access to the host amux state directory, SQLite files, or tmux socket

### Requirement: The context service exposes a minimal versioned interface
The service SHALL expose loopback-bound, versioned JSON operations for health, caller context, visible notes, note creation, event creation, current state, and bounded event waiting. Requests and responses MUST have explicit size limits, timeouts, content types, validation, and stable error codes. The prototype MUST NOT expose raw SQL, arbitrary filesystem access, arbitrary tmux commands, sandbox creation, or host shell execution.

#### Scenario: Client performs supported context operations
- **WHEN** an authenticated sandbox client invokes a supported `/v1` operation with valid input
- **THEN** the service returns a bounded JSON response representing the committed host-side result

#### Scenario: Client requests a host-control operation
- **WHEN** a sandbox client attempts an unknown operation or supplies data intended to execute SQL, shell, filesystem, or unrestricted tmux behavior
- **THEN** the service rejects the request without executing the supplied control data

#### Scenario: Request exceeds a bound
- **WHEN** a request exceeds the configured body, note, detail, result-count, or wait-time limit
- **THEN** the service returns a stable validation error and does not partially commit the request

### Requirement: Every sandbox request is authenticated and host-attributed
Amux SHALL mint a high-entropy capability token for each sandbox agent and store only its cryptographic hash in the host database. The token SHALL be bound to one active agent record, repository, workspace, task, pane, runtime, and permission set. The service MUST derive attribution from the token and MUST ignore or reject client attempts to override actor identity or broaden scope.

#### Scenario: Valid agent records an event
- **WHEN** a sandbox presents its active token and posts a valid state event
- **THEN** the service attributes the event to the token's registered agent and task regardless of identity fields in the request body

#### Scenario: Token is missing, invalid, expired, or revoked
- **WHEN** a request does not present a currently valid capability token
- **THEN** the service returns an authentication error and performs no context-store or tmux mutation

#### Scenario: Agent requests inaccessible context
- **WHEN** an authenticated agent requests agent-private notes belonging to another agent or context outside its allowed repository and workspace scope
- **THEN** the service returns no inaccessible records and reports a scope error where appropriate

### Requirement: Context visibility matches native amux semantics
The service SHALL apply the existing agent, task, and workspace note visibility rules and repository filtering used by native amux commands. Sandboxed agents SHALL see the same structured identity, team roster, visible notes, and resolved state that an equivalently scoped host agent would see. Full model conversation histories SHALL remain outside the shared context contract.

#### Scenario: Task-scoped note is shared
- **WHEN** one sandboxed or native agent publishes a task-scoped note
- **THEN** another authenticated agent in the same repository, workspace, and task can retrieve it while agents outside that scope cannot

#### Scenario: Agent-scoped note remains private
- **WHEN** an agent publishes an agent-scoped note
- **THEN** only that agent identity can retrieve it through either the host or sandbox context path

### Requirement: Sandbox events update host coordination state
After committing an authenticated sandbox event, the service SHALL update the corresponding host tmux pane state when the pane still exists and SHALL signal any matching waiters. Event identifiers SHALL be monotonic cursors so clients can resume reads without database replication or duplicate processing.

#### Scenario: Sandbox transitions to needs-input
- **WHEN** a sandbox hook posts a valid notification event
- **THEN** the event is durably recorded, the host pane resolves to `needs-input`, and a waiting host or sandbox client is released

#### Scenario: Client resumes after interruption
- **WHEN** a client requests events after its last acknowledged event identifier
- **THEN** the service returns only later visible events in identifier order and does not require a database snapshot

### Requirement: Sandbox clients preserve the context command experience
The prototype SHALL install a thin sandbox-local `amux` client that supports `ctx`, `notes`, `note`, and the applicable `event emit`, `event state`, and `event wait` commands by calling the context service. It SHALL support existing human-readable and JSON output modes where those modes exist and SHALL fail clearly for host-only control commands.

#### Scenario: Agent runs a supported command inside a sandbox
- **WHEN** a sandboxed agent invokes `amux ctx --json` or another supported context command
- **THEN** the thin client authenticates to the service and returns output compatible with the corresponding native command's documented fields

#### Scenario: Agent runs a host-only command inside a sandbox
- **WHEN** a sandboxed agent invokes a spawn, kill, clean, integrate, or monitor command through the thin client
- **THEN** the client refuses locally and explains that the operation must run on the host

### Requirement: Service and credential lifecycle fail closed
Sandbox spawning SHALL require a healthy context service reachable only through the configured localhost port and Docker policy path. Amux MUST NOT silently widen Docker's network policy. Capability material SHALL be delivered only to its sandbox, excluded from logs and command output, stored with restrictive permissions inside the VM, and revoked when the corresponding sandbox is removed.

#### Scenario: Context service becomes unavailable
- **WHEN** a sandbox context command cannot reach the service or the service cannot safely access its store
- **THEN** the client returns a clear transient failure and does not fall back to a database mount, local shadow database, or unauthenticated write

#### Scenario: Sandbox is removed
- **WHEN** amux successfully removes a sandbox
- **THEN** the associated capability is revoked and subsequent requests using the old token are rejected

#### Scenario: Logs are reviewed
- **WHEN** service, CLI, and sandbox bootstrap logs are inspected
- **THEN** plaintext context tokens and provider credentials are absent

### Requirement: Native behavior remains available during the prototype
Native host agents and existing amux commands SHALL continue to use the authoritative host store and tmux server when the Docker Sandbox runtime is not selected. The prototype context service SHALL reuse existing store and visibility functions rather than maintaining a second context database or divergent authorization implementation.

#### Scenario: Context service is not needed for a host grid
- **WHEN** a user operates an existing host-runtime workspace without starting a sandbox grid
- **THEN** current spawn, context, note, event, wait, monitor, worktree, and integration behavior remains available

