## Usage
```sh
amux spw myproj -p ~/Git/myproj -r 2 -c 2                                            # spawn workspace w/ 2x2 claude grid
amux spg myproj review -a codex:2                                                    # add a task (window) w/ 1x2 codex grid
amux spg myproj fix -r 2 -c 2 -a claude:3 -a codex                                   # mixed 2x2: 3 claude + 1 codex
amux spg myproj plan -a claude@opus/high -a codex@gpt-5.6-sol/xhigh                  # per-agent model + effort
amux spg myproj fix -a 'claude --model openai/gpt-5 --dangerously-skip-permissions'  # launch with additional params
amux lsw                                                                             # list workspaces
amux lsg myproj                                                                      # list tasks/agents in a workspace
amux send %9 "please review src/auth"                                                # wait for idle, send, confirm processing
amux messages --status undelivered                                                   # inspect messages
amux kg myproj review                                                                # kill a task
amux kw myproj                                                                       # kill a workspace
amux monitor                                                                         # live dashboard of every workspace/agent
amux monitor -W 160 -T 60                                                            # ...at 160 cols, 60 of them for the tree
```
- runs on a dedicated tmux server (socket `amux-root`); attach: `tmux -L amux-root attach -t myproj`

## Docker-sandbox
Prerequisites — Docker's `sbx` CLI:
```sh
sbx version
sbx policy init balanced
sbx policy allow network localhost:47317
```

A sandbox gets a private clone, not a worktree.

```
amux/<ws>/<task>/integration   task integration branch, on the host
amux/<ws>/<task>/<name>        the sandboxed agent's branch, inside the VM
```

```sh
amux spw myproj -p ~/Git/myproj --runtime docker-sandbox -a claude:2 -a codex:2
amux doctor -p ~/Git/myproj            # check sbx, its version, and the network policy
amux integrate myproj task0            # merge each sandbox's committed branch
amux kg myproj task0                   # stop the VMs, keep their state for reattach
amux kg myproj task0 --clean           # remove them; refuses a dirty sandbox
amux kg myproj task0 --clean --force   # ...and accept losing uncommitted work
```

## Apple-container

```sh
brew install container
container system start
amux doctor --runtime apple-container -p ~/Git/myproj
amux spw myproj -p ~/Git/myproj --runtime apple-container -a claude:2
amux kg myproj task0 --clean
```

## Skill

- `skills/amux/SKILL.md` — teaches agents how to use amux

## monitor

`amux monitor`

```sh
cd tui && npm install && npm run build
```

# examples
<table width="100%">
  <tr>
    <th>tui monitor</th>
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
