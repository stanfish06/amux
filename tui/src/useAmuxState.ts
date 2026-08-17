import { useCallback, useEffect, useMemo, useState } from 'react';
import { TmuxService } from './tmux.js';
import {
    AgentPaneInfo,
    AgentState,
    StatusMetrics,
    TreeNode,
    WorkspaceSessionInfo,
} from './types.js';

const PREVIEW_LINES = 12;

const workspaceNodeId = (workspace: string) => `w:${workspace}`;
const taskNodeId = (workspace: string, task: string) => `t:${workspace}:${task}`;
const agentNodeId = (workspace: string, task: string, paneId: string) =>
    `a:${workspace}:${task}:${paneId}`;

function agentMatches(agent: AgentPaneInfo, query: string): boolean {
    return [agent.name, agent.agentName, agent.label, agent.id, agent.state].some((field) =>
        field.toLowerCase().includes(query)
    );
}

function emptyStateCounts(): Record<AgentState, number> {
    // Every AgentState needs a key: Header indexes STATE_STYLE by state with no
    // fallback, so a state missing here is a crash rather than a blank.
    return {
        starting: 0,
        busy: 0,
        idle: 0,
        'needs-input': 0,
        stopped: 0,
        dead: 0,
        unknown: 0,
    };
}

export function useAmuxState(tmuxService: TmuxService, pollIntervalMs: number = 1500) {
    const [workspaces, setWorkspaces] = useState<WorkspaceSessionInfo[]>([]);
    const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
    const [expandedNodeIds, setExpandedNodeIds] = useState<Set<string>>(new Set());
    const [searchQuery, setSearchQuery] = useState<string>('');
    const [isSearching, setIsSearching] = useState<boolean>(false);
    const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
    const [isLoading, setIsLoading] = useState<boolean>(true);
    const [error, setError] = useState<string | null>(null);
    const [capturedOutput, setCapturedOutput] = useState<string[]>([]);

    const refresh = useCallback(async () => {
        try {
            const data = await tmuxService.fetchWorkspaces();
            setWorkspaces(data);
            setLastRefreshedAt(new Date());
            setError(null);

            setExpandedNodeIds((prev) => {
                if (prev.size > 0 || data.length === 0) return prev;
                return new Set(
                    data.flatMap((w) => [
                        workspaceNodeId(w.name),
                        ...w.tasks.map((t) => taskNodeId(w.name, t.name)),
                    ])
                );
            });
        } catch (err: any) {
            setError(err.message || 'Failed to fetch tmux workspaces');
        } finally {
            setIsLoading(false);
        }
    }, [tmuxService]);

    useEffect(() => {
        refresh();
        const timer = setInterval(refresh, pollIntervalMs);
        return () => clearInterval(timer);
    }, [refresh, pollIntervalMs]);



    const treeNodes = useMemo<TreeNode[]>(() => {
        const query = searchQuery.trim().toLowerCase();
        const nodes: TreeNode[] = [];

        for (const w of workspaces) {
            const taskNodes: TreeNode[] = [];

            for (const t of w.tasks) {
                const agentNodes: TreeNode[] = t.panes
                    .filter((a) => !query || agentMatches(a, query))
                    .map((a) => ({
                        id: agentNodeId(w.name, t.name, a.id),
                        type: 'agent',
                        info: a,
                        children: [],
                    }));

                if (query && agentNodes.length === 0 && !t.name.toLowerCase().includes(query)) continue;
                taskNodes.push({
                    id: taskNodeId(w.name, t.name),
                    type: 'task',
                    info: t,
                    children: agentNodes,
                });
            }

            if (query && taskNodes.length === 0 && !w.name.toLowerCase().includes(query)) continue;
            nodes.push({
                id: workspaceNodeId(w.name),
                type: 'workspace',
                info: w,
                children: taskNodes,
            });
        }

        return nodes;
    }, [workspaces, searchQuery]);



    const visibleNodes = useMemo<TreeNode[]>(() => {
        const showAll = searchQuery.trim() !== '';
        const flat: TreeNode[] = [];

        const traverse = (nodes: TreeNode[]) => {
            for (const node of nodes) {
                flat.push(node);
                if (node.children.length > 0 && (showAll || expandedNodeIds.has(node.id))) {
                    traverse(node.children);
                }
            }
        };

        traverse(treeNodes);
        return flat;
    }, [treeNodes, expandedNodeIds, searchQuery]);

    const selectedNode = useMemo(
        () => visibleNodes.find((n) => n.id === selectedNodeId) ?? null,
        [visibleNodes, selectedNodeId]
    );


    useEffect(() => {
        if (!selectedNode) {
            setSelectedNodeId(visibleNodes.length > 0 ? visibleNodes[0].id : null);
        }
    }, [visibleNodes, selectedNode]);

    useEffect(() => {
        if (selectedNode?.type !== 'agent') {
            setCapturedOutput([]);
            return;
        }
        tmuxService.capturePaneOutput(selectedNode.info.id, PREVIEW_LINES).then(setCapturedOutput);
    }, [selectedNode, tmuxService, lastRefreshedAt]);

    const toggleExpand = useCallback((nodeId: string) => {
        setExpandedNodeIds((prev) => {
            const next = new Set(prev);
            if (!next.delete(nodeId)) next.add(nodeId);
            return next;
        });
    }, []);

    const moveSelection = useCallback(
        (direction: 'up' | 'down') => {
            if (visibleNodes.length === 0) return;
            const currentIndex = visibleNodes.findIndex((n) => n.id === selectedNodeId);
            if (currentIndex === -1) {
                setSelectedNodeId(visibleNodes[0].id);
                return;
            }
            const nextIndex = currentIndex + (direction === 'down' ? 1 : -1);
            if (nextIndex >= 0 && nextIndex < visibleNodes.length) {
                setSelectedNodeId(visibleNodes[nextIndex].id);
            }
        },
        [visibleNodes, selectedNodeId]
    );

    const statusMetrics = useMemo<StatusMetrics>(() => {
        const counts = emptyStateCounts();
        const panes = workspaces.flatMap((w) => w.tasks.flatMap((t) => t.panes));
        for (const pane of panes) {
            counts[pane.state]++;
        }
        return { ...counts, total: panes.length };
    }, [workspaces]);

    return {
        selectedNodeId,
        selectedNode,
        expandedNodeIds,
        searchQuery,
        isSearching,
        lastRefreshedAt,
        isLoading,
        error,
        capturedOutput,
        visibleNodes,
        statusMetrics,
        setSearchQuery,
        setIsSearching,
        toggleExpand,
        moveSelection,
        refresh,
    };
}
