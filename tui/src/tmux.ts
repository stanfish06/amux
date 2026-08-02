import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {
  AgentState,
  AmuxEvent,
  EventKind,
  TaskWindowInfo,
  WorkspaceSessionInfo,
} from './types.js';

const execFileAsync = promisify(execFile);

const STATE_BY_KIND: Record<EventKind, AgentState> = {
  spawn: 'starting',
  busy: 'busy',
  stop: 'idle',
  notify: 'needs-input',
  exit: 'dead',
};

const MAX_EVENTS = 100;

const PANE_FORMAT = [
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

function isServerDown(stderr: unknown): boolean {
  return (
    typeof stderr === 'string' &&
    (stderr.includes('no server running') || stderr.includes('error connecting to'))
  );
}

function resolveState(
  paneOption: string,
  lastEvent: AmuxEvent | undefined,
  amuxName: string
): AgentState {
  const fromEvent = lastEvent && STATE_BY_KIND[lastEvent.kind];
  return (paneOption as AgentState) || fromEvent || (amuxName ? 'starting' : 'idle');
}

export class TmuxService {
  constructor(private readonly socketName: string = 'amux-root') {}

  public async fetchWorkspaces(): Promise<WorkspaceSessionInfo[]> {
    const rawOutput = await this.runTmux(['list-panes', '-a', '-F', PANE_FORMAT]);
    if (!rawOutput) {
      return [];
    }

    const latestEventByPane = await this.latestEventByPane();
    const workspaceById = new Map<string, WorkspaceSessionInfo>();
    const taskByKey = new Map<string, TaskWindowInfo>();

    for (const line of rawOutput.split('\n').filter(Boolean)) {
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

      let workspace = workspaceById.get(sessionId);
      if (!workspace) {
        workspace = { id: sessionId, name: sessionName, cwd: sessionPath || '.', tasks: [] };
        workspaceById.set(sessionId, workspace);
      }

      const windowIndex = parseInt(windowIndexStr, 10) || 0;
      const taskKey = `${sessionId}:${windowId}`;
      let task = taskByKey.get(taskKey);
      if (!task) {
        task = {
          id: windowId,
          name: windowName || `task${windowIndex}`,
          index: windowIndex,
          workspaceName: sessionName,
          panes: [],
        };
        taskByKey.set(taskKey, task);
        workspace.tasks.push(task);
      }

      const lastEvent = latestEventByPane.get(paneId);
      task.panes.push({
        id: paneId,
        name: nameOpt || '',
        agentName: agentOpt || paneCmd || 'unknown',
        label: labelOpt || paneId,
        state: resolveState(stateOpt, lastEvent, nameOpt || ''),
        cwd: paneCwd || '',
        lastEvent,
        taskName: task.name,
        workspaceName: sessionName,
      });
    }

    return Array.from(workspaceById.values());
  }

  public async capturePaneOutput(paneId: string, lines: number): Promise<string[]> {
    try {
      const out = await this.runTmux(['capture-pane', '-pt', paneId, '-S', `-${lines}`]);
      return out.split('\n').slice(-lines);
    } catch {
      return ['(Unable to capture pane output)'];
    }
  }

  private async runTmux(args: string[]): Promise<string> {
    try {
      const { stdout } = await execFileAsync('tmux', ['-L', this.socketName, ...args]);
      return stdout.trim();
    } catch (err: any) {
      if (isServerDown(err.stderr)) {
        return '';
      }
      throw err;
    }
  }

  private async latestEventByPane(): Promise<Map<string, AmuxEvent>> {
    const stateHome = process.env.XDG_STATE_HOME || path.join(os.homedir(), '.local', 'state');
    const latest = new Map<string, AmuxEvent>();

    let content: string;
    try {
      content = await fs.readFile(path.join(stateHome, 'amux', 'events.jsonl'), 'utf8');
    } catch {
      return latest;
    }

    for (const line of content.trim().split('\n').filter(Boolean).slice(-MAX_EVENTS)) {
      try {
        const event: AmuxEvent = JSON.parse(line);
        latest.set(event.pane, event);
      } catch {
      }
    }
    return latest;
  }
}
