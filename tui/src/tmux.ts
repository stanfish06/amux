import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {
  AgentPaneInfo,
  AgentState,
  AmuxEvent,
  TaskWindowInfo,
  WorkspaceSessionInfo,
} from './types.js';

const execFileAsync = promisify(execFile);

export class TmuxService {
  private socketName: string;

  constructor(socketName: string = 'amux-root') {
    this.socketName = socketName;
  }

  public getSocketName(): string {
    return this.socketName;
  }

  private async runTmux(args: string[]): Promise<string> {
    try {
      const { stdout } = await execFileAsync('tmux', ['-L', this.socketName, ...args]);
      return stdout.trim();
    } catch (err: any) {
      // If server is not running, return empty string instead of throwing
      if (err.stderr && err.stderr.includes('no server running')) {
        return '';
      }
      throw err;
    }
  }

  public async getEventsLog(): Promise<AmuxEvent[]> {
    const xdgState = process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state');
    const eventsPath = path.join(xdgState, 'amux', 'events.jsonl');

    try {
      const content = await fs.readFile(eventsPath, 'utf8');
      const lines = content.trim().split('\n').filter(Boolean);
      const events: AmuxEvent[] = [];

      for (const line of lines.slice(-100)) {
        try {
          const parsed = JSON.parse(line);
          events.push({
            ts: parsed.ts,
            kind: parsed.kind,
            pane: parsed.pane,
            agent: parsed.agent || '',
            detail: parsed.detail || '',
          });
        } catch {
          // ignore malformed line
        }
      }
      return events;
    } catch {
      return [];
    }
  }

  public async fetchWorkspaces(): Promise<WorkspaceSessionInfo[]> {
    // 1. List sessions: #{session_id}:#{session_name}:#{session_path}
    const rawSessions = await this.runTmux(['list-sessions', '-F', '#{session_id}:#{session_name}:#{session_path}']);
    if (!rawSessions) {
      return [];
    }

    const events = await this.getEventsLog();
    // Build map of latest event per pane
    const latestEventByPane = new Map<string, AmuxEvent>();
    for (const ev of events) {
      latestEventByPane.set(ev.pane, ev);
    }

    const sessionLines = rawSessions.split('\n').filter(Boolean);
    const workspaces: WorkspaceSessionInfo[] = [];

    for (const line of sessionLines) {
      const [sessionId, sessionName, sessionPath] = line.split(':');
      if (!sessionId || !sessionName) continue;

      // 2. List windows for this session: #{window_id}:#{window_index}:#{window_name}
      const rawWindows = await this.runTmux([
        'list-windows',
        '-t',
        sessionName,
        '-F',
        '#{window_id}:#{window_index}:#{window_name}',
      ]);

      const windowLines = rawWindows.split('\n').filter(Boolean);
      const tasks: TaskWindowInfo[] = [];

      for (const wLine of windowLines) {
        const [windowId, windowIndexStr, windowName] = wLine.split(':');
        if (!windowId) continue;
        const windowIndex = parseInt(windowIndexStr, 10) || 0;

        // 3. List panes for this window: #{pane_id}:#{pane_current_path}:#{pane_current_command}
        const rawPanes = await this.runTmux([
          'list-panes',
          '-t',
          `${sessionName}:${windowIndex}`,
          '-F',
          '#{pane_id}:#{pane_current_path}:#{pane_current_command}',
        ]);

        const paneLines = rawPanes.split('\n').filter(Boolean);
        const panes: AgentPaneInfo[] = [];

        for (const pLine of paneLines) {
          const parts = pLine.split(':');
          const paneId = parts[0];
          const paneCwd = parts[1] || '';
          const paneCmd = parts[2] || '';

          if (!paneId) continue;

          // Query options on pane
          const agentOpt = await this.getPaneOption(paneId, '@amux_agent');
          const labelOpt = await this.getPaneOption(paneId, '@amux_label');
          const nameOpt = await this.getPaneOption(paneId, '@amux_name');
          const stateOpt = await this.getPaneOption(paneId, '@amux_state');

          const agentName = agentOpt || paneCmd || 'unknown';
          const name = nameOpt || '';
          const label = labelOpt || paneId;
          
          let state: AgentState = (stateOpt as AgentState) || 'unknown';
          const lastEv = latestEventByPane.get(paneId);
          if (state === 'unknown' && lastEv) {
            state = this.stateFromKind(lastEv.kind);
          }
          if (state === 'unknown') {
            state = name ? 'starting' : 'idle';
          }

          panes.push({
            id: paneId,
            name,
            agentName,
            label,
            state,
            cwd: paneCwd,
            isAgent: ['claude', 'codex'].includes(agentName),
            lastEvent: lastEv,
            taskName: windowName || `task${windowIndex}`,
            workspaceName: sessionName,
          });
        }

        tasks.push({
          id: windowId,
          name: windowName || `task${windowIndex}`,
          index: windowIndex,
          workspaceName: sessionName,
          panes,
        });
      }

      workspaces.push({
        id: sessionId,
        name: sessionName,
        cwd: sessionPath || '',
        tasks,
      });
    }

    return workspaces;
  }

  private async getPaneOption(paneId: string, optionName: string): Promise<string> {
    try {
      const out = await this.runTmux(['show-options', '-pqv', '-t', paneId, optionName]);
      return out.trim();
    } catch {
      return '';
    }
  }

  private stateFromKind(kind: string): AgentState {
    switch (kind) {
      case 'spawn': return 'starting';
      case 'busy': return 'busy';
      case 'stop': return 'idle';
      case 'notify': return 'needs-input';
      case 'exit': return 'dead';
      default: return 'unknown';
    }
  }

  public async capturePaneOutput(paneId: string, lines: number = 15): Promise<string[]> {
    try {
      const out = await this.runTmux(['capture-pane', '-pt', paneId, '-S', `-${lines}`]);
      return out.split('\n');
    } catch {
      return ['(Unable to capture pane output)'];
    }
  }

  public async spawnWorkspace(
    workspaceName: string,
    pathDir?: string,
    taskName?: string,
    rows?: number,
    cols?: number,
    agents?: string[]
  ): Promise<void> {
    const args = ['spw', workspaceName];
    if (pathDir) args.push('-p', pathDir);
    if (taskName) args.push('-t', taskName);
    if (rows) args.push('-r', rows.toString());
    if (cols) args.push('-c', cols.toString());
    if (agents && agents.length > 0) {
      for (const a of agents) {
        args.push('-a', a);
      }
    }
    await execFileAsync('amux', ['-L', this.socketName, ...args]);
  }

  public async spawnTask(
    workspaceName: string,
    taskName: string,
    pathDir?: string,
    rows?: number,
    cols?: number,
    agents?: string[]
  ): Promise<void> {
    const args = ['spg', workspaceName, taskName];
    if (pathDir) args.push('-p', pathDir);
    if (rows) args.push('-r', rows.toString());
    if (cols) args.push('-c', cols.toString());
    if (agents && agents.length > 0) {
      for (const a of agents) {
        args.push('-a', a);
      }
    }
    await execFileAsync('amux', ['-L', this.socketName, ...args]);
  }

  public async killWorkspace(workspaceName: string): Promise<void> {
    await execFileAsync('amux', ['-L', this.socketName, 'kw', workspaceName]);
  }

  public async killTask(workspaceName: string, taskName: string): Promise<void> {
    await execFileAsync('amux', ['-L', this.socketName, 'kg', workspaceName, taskName]);
  }

  public async sendKeysToPane(paneId: string, keys: string): Promise<void> {
    await this.runTmux(['send-keys', '-t', paneId, keys, 'Enter']);
  }
}
