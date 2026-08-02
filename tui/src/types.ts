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
  id: string; // e.g. "%1"
  name: string; // e.g. "brave-hawk"
  agentName: string; // e.g. "claude", "codex"
  label: string; // e.g. "r0c0"
  state: AgentState;
  cwd: string;
  isAgent: boolean;
  lastEvent?: AmuxEvent;
  taskName: string;
  workspaceName: string;
}

export interface TaskWindowInfo {
  id: string; // e.g. "@0"
  name: string; // task name e.g. "task0"
  index: number;
  workspaceName: string;
  panes: AgentPaneInfo[];
}

export interface WorkspaceSessionInfo {
  id: string; // e.g. "$0"
  name: string; // workspace name e.g. "myproj"
  cwd: string;
  tasks: TaskWindowInfo[];
}

export type TreeNodeType = 'workspace' | 'task' | 'agent';

export interface TreeNode {
  id: string; // unique node id e.g. "w:myproj", "t:myproj:task0", "a:%1"
  type: TreeNodeType;
  label: string;
  workspaceName: string;
  taskName?: string;
  paneId?: string;
  info?: WorkspaceSessionInfo | TaskWindowInfo | AgentPaneInfo;
  children: TreeNode[];
  depth: number;
}

export interface AppState {
  socketName: string;
  workspaces: WorkspaceSessionInfo[];
  recentEvents: AmuxEvent[];
  selectedNodeId: string | null;
  expandedNodeIds: Set<string>;
  searchQuery: string;
  isSearching: boolean;
  lastRefreshedAt: Date | null;
  isLoading: boolean;
  error: string | null;
  capturedOutput: string[];
}
