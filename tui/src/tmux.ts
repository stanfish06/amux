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

  /**
   * Fast single-pass batch query fetching all sessions, windows, panes, and amux options
   * in a single tmux invocation.
   */
  public async fetchWorkspaces(): Promise<WorkspaceSessionInfo[]> {
    const format = [
      '#{session_id}',
      '#{session_name}',
      '#{session_path}',
      '#{window_id}',
      '#{window_index}',
      '#{window_name}',
      '#{pane_id}',
      '#{pane_current_path}',
      '#{pane_current_command}',
      '#{@amux_agent}',
      '#{@amux_label}',
      '#{@amux_name}',
      '#{@amux_state}',
    ].join('|');

    const rawOutput = await this.runTmux(['list-panes', '-a', '-F', format]);
    if (!rawOutput) {
      return [];
    }

    const events = await this.getEventsLog();
    const latestEventByPane = new Map<string, AmuxEvent>();
    for (const ev of events) {
      latestEventByPane.set(ev.pane, ev);
    }

    const workspaceMap = new Map<string, WorkspaceSessionInfo>();
    const taskMap = new Map<string, TaskWindowInfo>();

    const lines = rawOutput.split('\n').filter(Boolean);

    for (const line of lines) {
      const [
        sessionId,
        sessionName,
        sessionPath,
        windowId,
        windowIndexStr,
        windowName,
        paneId,
        paneCwd,
        paneCmd,
        agentOpt,
        labelOpt,
        nameOpt,
        stateOpt,
      ] = line.split('|');

      if (!sessionId || !sessionName || !windowId || !paneId) continue;

      const windowIndex = parseInt(windowIndexStr, 10) || 0;
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

      // Ensure Workspace object exists
      let ws = workspaceMap.get(sessionId);
      if (!ws) {
        ws = {
          id: sessionId,
          name: sessionName,
          cwd: sessionPath || '.',
          tasks: [],
        };
        workspaceMap.set(sessionId, ws);
      }

      // Ensure Task object exists
      const taskKey = `${sessionId}:${windowId}`;
      let task = taskMap.get(taskKey);
      if (!task) {
        task = {
          id: windowId,
          name: windowName || `task${windowIndex}`,
          index: windowIndex,
          workspaceName: sessionName,
          panes: [],
        };
        taskMap.set(taskKey, task);
        ws.tasks.push(task);
      }

      task.panes.push({
        id: paneId,
        name,
        agentName,
        label,
        state,
        cwd: paneCwd || '',
        isAgent: ['claude', 'codex'].includes(agentName),
        lastEvent: lastEv,
        taskName: task.name,
        workspaceName: sessionName,
      });
    }

    return Array.from(workspaceMap.values());
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
}
