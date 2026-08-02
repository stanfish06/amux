import { useCallback, useEffect, useMemo, useState } from 'react';
import { TmuxService } from './tmux.js';
import {
  AmuxEvent,
  TaskWindowInfo,
  TreeNode,
  WorkspaceSessionInfo,
} from './types.js';

export function useAmuxState(tmuxService: TmuxService, pollIntervalMs: number = 1500) {
  const [workspaces, setWorkspaces] = useState<WorkspaceSessionInfo[]>([]);
  const [recentEvents, setRecentEvents] = useState<AmuxEvent[]>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [expandedNodeIds, setExpandedNodeIds] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [isSearching, setIsSearching] = useState<boolean>(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [capturedOutput, setCapturedOutput] = useState<string[]>([]);

  // Core refresh function
  const refresh = useCallback(async () => {
    try {
      const data = await tmuxService.fetchWorkspaces();
      const events = await tmuxService.getEventsLog();
      setWorkspaces(data);
      setRecentEvents(events);
      setLastRefreshedAt(new Date());
      setError(null);

      setExpandedNodeIds((prev) => {
        if (prev.size === 0 && data.length > 0) {
          const next = new Set<string>();
          for (const w of data) {
            next.add(`w:${w.name}`);
            for (const t of w.tasks) {
              next.add(`t:${w.name}:${t.name}`);
            }
          }
          return next;
        }
        return prev;
      });
    } catch (err: any) {
      setError(err.message || 'Failed to fetch tmux workspaces');
    } finally {
      setIsLoading(false);
    }
  }, [tmuxService]);

  // Initial fetch + interval polling
  useEffect(() => {
    refresh();
    const timer = setInterval(() => {
      refresh();
    }, pollIntervalMs);
    return () => clearInterval(timer);
  }, [refresh, pollIntervalMs]);

  // Build Hierarchical Tree Nodes
  const treeNodes = useMemo<TreeNode[]>(() => {
    const nodes: TreeNode[] = [];
    const query = searchQuery.trim().toLowerCase();

    for (const w of workspaces) {
      const wNodeId = `w:${w.name}`;
      const taskNodes: TreeNode[] = [];

      for (const t of w.tasks) {
        const tNodeId = `t:${w.name}:${t.name}`;
        const agentNodes: TreeNode[] = [];

        for (const a of t.panes) {
          const aNodeId = `a:${w.name}:${t.name}:${a.id}`;
          const matchesQuery =
            !query ||
            a.name.toLowerCase().includes(query) ||
            a.agentName.toLowerCase().includes(query) ||
            a.label.toLowerCase().includes(query) ||
            a.id.toLowerCase().includes(query) ||
            a.state.toLowerCase().includes(query);

          if (matchesQuery) {
            agentNodes.push({
              id: aNodeId,
              type: 'agent',
              label: `${a.name || 'unnamed'} [${a.agentName}]`,
              workspaceName: w.name,
              taskName: t.name,
              paneId: a.id,
              info: a,
              children: [],
              depth: 2,
            });
          }
        }

        const taskMatches = !query || t.name.toLowerCase().includes(query) || agentNodes.length > 0;
        if (taskMatches) {
          taskNodes.push({
            id: tNodeId,
            type: 'task',
            label: `${t.name} (${t.panes.length} agents)`,
            workspaceName: w.name,
            taskName: t.name,
            info: t,
            children: agentNodes,
            depth: 1,
          });
        }
      }

      const wsMatches = !query || w.name.toLowerCase().includes(query) || taskNodes.length > 0;
      if (wsMatches) {
        nodes.push({
          id: wNodeId,
          type: 'workspace',
          label: `${w.name} @ ${w.cwd}`,
          workspaceName: w.name,
          info: w,
          children: taskNodes,
          depth: 0,
        });
      }
    }

    return nodes;
  }, [workspaces, searchQuery]);

  // Flatten visible tree nodes based on expanded state
  const visibleNodes = useMemo<TreeNode[]>(() => {
    const flat: TreeNode[] = [];

    function traverse(nodes: TreeNode[]) {
      for (const node of nodes) {
        flat.push(node);
        if (node.children.length > 0 && (expandedNodeIds.has(node.id) || searchQuery.trim() !== '')) {
          traverse(node.children);
        }
      }
    }

    traverse(treeNodes);
    return flat;
  }, [treeNodes, expandedNodeIds, searchQuery]);

  // Ensure valid selection
  useEffect(() => {
    if (visibleNodes.length > 0) {
      if (!selectedNodeId || !visibleNodes.some((n) => n.id === selectedNodeId)) {
        setSelectedNodeId(visibleNodes[0].id);
      }
    } else {
      setSelectedNodeId(null);
    }
  }, [visibleNodes, selectedNodeId]);

  // Selected node info
  const selectedNode = useMemo(() => {
    return visibleNodes.find((n) => n.id === selectedNodeId) || null;
  }, [visibleNodes, selectedNodeId]);

  // Capture pane output when an agent node is selected
  useEffect(() => {
    if (selectedNode && selectedNode.type === 'agent' && selectedNode.paneId) {
      tmuxService.capturePaneOutput(selectedNode.paneId, 16).then((lines) => {
        setCapturedOutput(lines);
      });
    } else {
      setCapturedOutput([]);
    }
  }, [selectedNode, tmuxService, lastRefreshedAt]);

  const toggleExpand = useCallback((nodeId: string) => {
    setExpandedNodeIds((prev) => {
      const next = new Set(prev);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
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
      if (direction === 'up' && currentIndex > 0) {
        setSelectedNodeId(visibleNodes[currentIndex - 1].id);
      } else if (direction === 'down' && currentIndex < visibleNodes.length - 1) {
        setSelectedNodeId(visibleNodes[currentIndex + 1].id);
      }
    },
    [visibleNodes, selectedNodeId]
  );

  const statusMetrics = useMemo(() => {
    let totalPanes = 0;
    let starting = 0;
    let busy = 0;
    let idle = 0;
    let needsInput = 0;
    let dead = 0;

    for (const w of workspaces) {
      for (const t of w.tasks) {
        for (const p of t.panes) {
          totalPanes++;
          switch (p.state) {
            case 'starting': starting++; break;
            case 'busy': busy++; break;
            case 'idle': idle++; break;
            case 'needs-input': needsInput++; break;
            case 'dead': dead++; break;
          }
        }
      }
    }

    return { totalPanes, starting, busy, idle, needsInput, dead };
  }, [workspaces]);

  return {
    workspaces,
    recentEvents,
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
    setSelectedNodeId,
    setSearchQuery,
    setIsSearching,
    toggleExpand,
    moveSelection,
    refresh,
  };
}
