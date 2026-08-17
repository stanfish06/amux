export type EventKind = 'spawn' | 'busy' | 'stop' | 'notify' | 'exit';
// 'stopped' has no event kind: it comes from the execution row when a sandbox
// has been stopped but not cleaned up. 'unknown' is this side's own fallback and
// has no Python counterpart.
export type AgentState =
    | 'starting'
    | 'busy'
    | 'idle'
    | 'needs-input'
    | 'stopped'
    | 'dead'
    | 'unknown';

export interface AmuxEvent {
    ts: number;
    kind: EventKind;
    pane: string;
    agent?: string;
    detail?: string;
}

export interface AgentPaneInfo {
    id: string;
    name: string;
    agentName: string;
    label: string;
    state: AgentState;
    cwd: string;
    lastEvent?: AmuxEvent;
    // Present only for a non-host runtime, matching `amux event state --json`,
    // which omits these keys entirely for host agents.
    runtime?: string;
    runtimeStatus?: string;
    sandboxName?: string;
    taskName: string;
    workspaceName: string;
}

export interface TaskWindowInfo {
    id: string;
    name: string;
    index: number;
    workspaceName: string;
    panes: AgentPaneInfo[];
}

export interface WorkspaceSessionInfo {
    id: string;
    name: string;
    cwd: string;
    tasks: TaskWindowInfo[];
}

export type TreeNode =
    | { id: string; type: 'workspace'; info: WorkspaceSessionInfo; children: TreeNode[] }
    | { id: string; type: 'task'; info: TaskWindowInfo; children: TreeNode[] }
    | { id: string; type: 'agent'; info: AgentPaneInfo; children: TreeNode[] };

export type StatusMetrics = Record<AgentState, number> & { total: number };
