export type EventKind = 'spawn' | 'busy' | 'stop' | 'notify' | 'exit';
export type AgentState = 'starting' | 'busy' | 'idle' | 'needs-input' | 'dead' | 'unknown';

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
