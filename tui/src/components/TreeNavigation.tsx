import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import { AgentPaneInfo, AgentState, TaskWindowInfo, TreeNode, WorkspaceSessionInfo } from '../types.js';

interface TreeNavigationProps {
  nodes: TreeNode[];
  selectedNodeId: string | null;
  expandedNodeIds: Set<string>;
  width?: number;
  isLoading?: boolean;
  lastRefreshedAt?: Date | null;
  socketName?: string;
}

export const TreeNavigation: React.FC<TreeNavigationProps> = ({
  nodes,
  selectedNodeId,
  expandedNodeIds,
  width = 44,
  isLoading,
  lastRefreshedAt,
  socketName = 'amux-root',
}) => {
  if (isLoading && !lastRefreshedAt) {
    return (
      <Box
        flexDirection="column"
        borderStyle="single"
        borderColor="cyan"
        width={width}
        padding={1}
      >
        <Text color="cyan">
          <Spinner type="dots" /> Loading amux workspaces ({socketName})...
        </Text>
      </Box>
    );
  }

  if (nodes.length === 0) {
    return (
      <Box
        flexDirection="column"
        borderStyle="single"
        borderColor="gray"
        width={width}
        padding={1}
      >
        <Text color="gray" italic>
          No active workspaces found on socket: {socketName}.
        </Text>
      </Box>
    );
  }

  return (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor="blue"
      width={width}
      paddingX={1}
    >
      <Box marginBottom={0} justifyContent="space-between">
        <Text color="cyan" bold>
          WORKSPACES & AGENTS
        </Text>
        <Text color="gray">[{nodes.length}]</Text>
      </Box>

      <Box flexDirection="column" marginTop={0}>
        {nodes.map((node) => {
          const isSelected = node.id === selectedNodeId;
          const isExpanded = expandedNodeIds.has(node.id);

          return (
            <TreeNodeRow
              key={node.id}
              node={node}
              isSelected={isSelected}
              isExpanded={isExpanded}
            />
          );
        })}
      </Box>
    </Box>
  );
};

interface TreeNodeRowProps {
  node: TreeNode;
  isSelected: boolean;
  isExpanded: boolean;
}

const TreeNodeRow: React.FC<TreeNodeRowProps> = ({ node, isSelected, isExpanded }) => {
  const indent = '  '.repeat(node.depth);

  if (node.type === 'workspace') {
    const ws = node.info as WorkspaceSessionInfo;
    const taskCount = ws?.tasks.length || 0;
    const totalAgents = ws?.tasks.reduce((sum, t) => sum + t.panes.length, 0) || 0;

    return (
      <Box gap={1}>
        <Text color={isSelected ? 'cyan' : 'gray'}>
          {isSelected ? '▶' : ' '}
        </Text>
        <Text wrap="truncate">
          {indent}
          <Text color="yellow" bold>
            {isExpanded ? '▼ 📁' : '▶ 📁'} {ws.name}
          </Text>{' '}
          <Text color="gray">({totalAgents} agents in {taskCount} tasks)</Text>
        </Text>
      </Box>
    );
  }

  if (node.type === 'task') {
    const task = node.info as TaskWindowInfo;
    const agentCount = task?.panes.length || 0;

    return (
      <Box gap={1}>
        <Text color={isSelected ? 'cyan' : 'gray'}>
          {isSelected ? '▶' : ' '}
        </Text>
        <Text wrap="truncate">
          {indent}
          <Text color="magenta" bold>
            {isExpanded ? '▼ 📋' : '▶ 📋'} {task.name}
          </Text>{' '}
          <Text color="gray">[{agentCount}]</Text>
        </Text>
      </Box>
    );
  }

  // Agent Node
  const agent = node.info as AgentPaneInfo;
  return (
    <Box gap={1}>
      <Text color={isSelected ? 'cyan' : 'gray'}>
        {isSelected ? '▶' : ' '}
      </Text>
      <Box gap={1}>
        <Text>{indent}🤖 </Text>
        <Text color={isSelected ? 'white' : 'brightWhite'} bold={isSelected}>
          {agent.name || 'agent'}
        </Text>
        <Text color="gray">[{agent.agentName}]</Text>
        <StateBadge state={agent.state} />
        <Text color="gray">{agent.id}</Text>
      </Box>
    </Box>
  );
};

export const StateBadge: React.FC<{ state: AgentState }> = ({ state }) => {
  switch (state) {
    case 'idle':
      return <Text color="green" bold>[IDLE]</Text>;
    case 'busy':
      return <Text color="yellow" bold>[BUSY]</Text>;
    case 'needs-input':
      return <Text color="magenta" bold>[INPUT?]</Text>;
    case 'starting':
      return <Text color="cyan" bold>[START]</Text>;
    case 'dead':
      return <Text color="red" bold>[DEAD]</Text>;
    default:
      return <Text color="gray">[UNKNW]</Text>;
  }
};
