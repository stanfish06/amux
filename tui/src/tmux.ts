import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import {
  AgentState,
  AmuxEvent,
  EventKind,
  TaskWindowInfo,
  WorkspaceSessionInfo,
} from './types.js';

const execFileAsync = promisify(execFile);

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

interface PaneStatus {
  pane: string;
  state: AgentState;
  last_event: { ts: number; kind: EventKind; detail?: string } | null;
}

export class TmuxService {
  constructor(private readonly socketName: string = 'amux-root') {}

  public async fetchWorkspaces(): Promise<WorkspaceSessionInfo[]> {
    const rawOutput = await this.runTmux(['list-panes', '-a', '-F', PANE_FORMAT]);
    if (!rawOutput) {
      return [];
    }

    const statusByPane = await this.paneStatuses();
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

      const status = statusByPane.get(paneId);
      const lastEvent: AmuxEvent | undefined = status?.last_event
        ? { ...status.last_event, pane: paneId }
        : undefined;
      task.panes.push({
        id: paneId,
        name: nameOpt || '',
        agentName: agentOpt || paneCmd || 'unknown',
        label: labelOpt || paneId,
        state: status?.state || (stateOpt as AgentState) || 'unknown',
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

  // Shell out rather than open context.db from Node: schema and migration live
  // on the Python side. $AMUX_BIN is set by `amux monitor` so the frozen binary
  // finds itself; a bare `amux` on PATH covers `npm run dev`.
  private async paneStatuses(): Promise<Map<string, PaneStatus>> {
    const byPane = new Map<string, PaneStatus>();
    try {
      const { stdout } = await execFileAsync(
        process.env.AMUX_BIN || 'amux',
        ['-L', this.socketName, 'event', 'state', '--json'],
        { maxBuffer: 4 * 1024 * 1024 }
      );
      for (const status of JSON.parse(stdout) as PaneStatus[]) {
        byPane.set(status.pane, status);
      }
    } catch {
    }
    return byPane;
  }
}
