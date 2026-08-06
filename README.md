# project goals
- [ ] manage agent spawn and close in terminal multiplexers
- [ ] send/read messages across panes/windows, human->agent and agent->agent
- [ ] persistence, session/window management, automation
- Design
    - sessions = workspaces
    - windows = tasks
    - panes = agents

# usage
```sh
amux spw myproj -p ~/Git/myproj -r 2 -c 2   # spawn workspace w/ 2x2 claude grid
amux spg myproj review -a codex:2           # add a task (window) w/ 1x2 codex grid
amux spg myproj fix -a claude:3 -a codex    # mixed 2x2: 3 claude + 1 codex (auto shape)
amux lsw                                    # list workspaces
amux lsg myproj                             # list tasks/agents in a workspace
amux kg myproj review                       # kill a task
amux kw myproj                              # kill a workspace
amux monitor                                # live dashboard of every workspace/agent
amux monitor -W 160 -T 60                   # ...at 160 cols, 60 of them for the tree
```
- runs on a dedicated tmux server (socket `amux-root`); attach: `tmux -L amux-root attach -t myproj`

# architecture

```mermaid
flowchart TB
    accTitle: amux Layered Architecture
    accDescr: The CLI and monitor TUI drive one of two runtimes, host tmux panes or Docker sandbox microVMs, agents report state either directly or through an authenticated loopback context service, and both paths converge on one host-side context database.

    subgraph frontend ["Frontend"]
        cli["amux CLI<br/>spw · spg · kg · kw · integrate<br/>note · ctx · doctor · context-service"]
        tui["amux monitor<br/>read-only Ink TUI, tui/"]
    end

    subgraph runtime ["Runtime"]
        proto{{"Runtime protocol<br/>runtime.py"}}
        host["HostRuntime<br/>one tmux pane per agent, socket amux-root"]
        vm["SandboxRuntime<br/>one sbx microVM per agent, private clone"]
        agent["claude / codex"]
    end

    subgraph comms ["Communication"]
        hooks["agent hooks<br/>spawn · busy · stop · notify · exit"]
        emit["amux event emit<br/>events.py, in process on the host"]
        shim["in-VM amux shim<br/>sandbox_client.py, context subset only"]
        svc["context service<br/>loopback HTTP :47317, bearer capability<br/>context:read · notes:write · events:write"]
        bus["tmux bus<br/>@amux_state option + wait-for channel"]
    end

    subgraph contextl ["Context"]
        store["store.py<br/>schema · migration · token hashing"]
        db[("context.db<br/>events · notes · worktrees · tokens")]
        trees["git worktrees<br/>amux/ws/task/name off the integration branch"]
    end

    cli --> proto
    cli --> store
    cli -->|"amux context-service start"| svc
    tui -.->|"poll tmux"| bus
    tui -.->|"amux event state --json"| store
    proto --> host
    proto --> vm
    host --> agent
    vm --> agent
    host --> trees
    vm -->|"integrate: fetch the sandbox remote"| trees
    trees --> store

    agent --> hooks
    hooks -->|host| emit
    hooks -->|sandboxed| shim
    shim -->|"HTTP + bearer token"| svc
    svc --> store
    emit --> store
    emit --> bus
    svc -.->|"if the pane is alive"| bus
    store --> db

    classDef frontend_style fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#3b0764
    classDef runtime_style fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef comms_style fill:#fef9c3,stroke:#ca8a04,stroke-width:2px,color:#713f12
    classDef context_style fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef guard_style fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d

    class cli,tui frontend_style
    class proto,host,agent runtime_style
    class hooks,emit,shim,bus comms_style
    class store,db,trees context_style
    class vm,svc guard_style
```

# runtimes

Two execution backends. `host` is the default and is unchanged: the agent runs
on your machine, in its own git worktree, on the `amux-root` tmux server.

`docker-sandbox` is opt-in and a prototype. Each agent runs inside a Docker
Sandboxes microVM instead, so it cannot reach your files, processes,
credentials, or Docker daemon outside its own clone. amux keeps every
coordination concern on the host: workspace, task, roster, notes, events,
worktrees, integration.

> **Status.** Wired end to end and usable: `spw`/`spg` accept `--runtime
> docker-sandbox` with `--cpus`, `--memory`, `--share-skills` and
> `--context-port`, `amux doctor` checks the prerequisites without changing
> anything, and `kg`/`kw` take `--clean` and `--force`. Still called a prototype
> because the smoke test in `docs/sandbox-smoke-test.md` is a manual procedure
> against a real authenticated host — an offline suite cannot verify hooks that
> only fire inside a live VM.

## two rules that define the boundary

1. **The context database never leaves the host.** amux does not mount, copy, or
   synchronise `context.db`, its WAL/shm files, the amux state directory, or the
   tmux socket into a sandbox — not read-only, not ever. A sandboxed agent
   reaches context only through an authenticated loopback HTTP service, and when
   that service is unreachable it fails loudly rather than falling back to a
   mounted database, a local shadow copy, or an unauthenticated write.
2. **A sandbox cannot control the host.** `spw`, `spg`, `kg`, `kw`, `integrate`,
   `monitor`, `lsw` and `lsg` are not expressible in the capability vocabulary at
   all, so no sandbox token can be escalated into them. The in-VM `amux` refuses
   them locally, before it even reads its credentials.

## prerequisites

Docker's `sbx` is an optional external tool, not a Python dependency. amux
detects it at run time and never installs it, signs you in, or changes your
Docker policy.

```sh
sbx version                       # amux requires >= 0.37.0
sbx policy init balanced          # one-time, host-wide; amux will not do this for you
sbx policy allow network localhost:47317   # only if preflight tells you to
```

The policy commands are yours to run deliberately. amux checks both and prints
the exact remediation, because widening a host-wide network policy on your
behalf is not its call.

## clone, commit, integrate

A sandbox gets a private clone, not your worktree — Docker mounts the repository
read-only and the agent works on its own copy, which is why normal git works
inside and why nothing it does can touch your checkout.

```
amux/<ws>/<task>/integration   task integration branch, on the host
amux/<ws>/<task>/<name>        the sandboxed agent's branch, inside the VM
```

**Uncommitted work does not exist to the rest of amux.** The agent's commits
become reachable on the host through a `sandbox-<name>` git remote that Docker
creates; `amux integrate` fetches that branch and merges it into the task
integration worktree with `--no-ff`. It never imports a dirty working tree.

```sh
amux spw myproj -p ~/Git/myproj --runtime docker-sandbox -a claude:2 -a codex:2
amux doctor -p ~/Git/myproj        # check sbx, its version, and the network policy
amux integrate myproj task0        # merge each sandbox's committed branch
amux kg myproj task0               # stop the VMs, keep their state for reattach
amux kg myproj task0 --clean       # remove them; refuses a dirty sandbox
amux kg myproj task0 --clean --force   # ...and accept losing uncommitted work
```

## more

- `skills/amux/SKILL.md` — the agent-facing guide: what changes inside a
  sandbox, the trust model, and troubleshooting. Bootstrap installs it into every
  sandbox alongside the shim, because `--no-share-skills` means a sandboxed agent
  cannot read the copy on your host. With `--share-skills` amux leaves it alone —
  your skill directory is shared in, and writing there would cross the boundary
  the wrong way. On a host, `make install_skills` links this same file.
- `docs/sandbox-smoke-test.md` — the manual verification procedure, run against
  a disposable repository on a real authenticated host.

## monitor

`amux monitor` opens a read-only dashboard (workspace/task/agent tree, agent
detail, live pane preview). It is a Node app under `tui/`, so build it once:

```sh
cd tui && npm install && npm run build
```

# examples
<table width="100%">
  <tr>
    <th>tui monior</th>
  </tr>
  <tr>
    <td width="100%">
      <img src="./tui.png" width="600" />
    </td>
  </tr>
  <tr>
    <th>context database</th>
  </tr>
  <tr>
    <td width="100%">
      <img src="./db.png" width="600" />
    </td>
  </tr>
  <tr>
    <th>spawn 2 claude and 2 codex</th>
  </tr>
  <tr>
    <td width="100%">
      <img src="./spawn.png" width="600" />
    </td>
  </tr>
</table>
