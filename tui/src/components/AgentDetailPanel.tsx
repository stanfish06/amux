import React from 'react';
import { Box, Text } from 'ink';
import { StateBadge } from './TreeNavigation.js';
import { AgentPaneInfo, TaskWindowInfo, TreeNode, WorkspaceSessionInfo } from '../types.js';

interface AgentDetailPanelProps {
  selectedNode: TreeNode | null;
}

export const AgentDetailPanel: React.FC<AgentDetailPanelProps> = ({ selectedNode }) => {
  if (!selectedNode) {
    return (
      <Box
        flexDirection="column"
        borderStyle="single"
        borderColor="gray"
        flexGrow={1}
        padding={1}
      >
        <Text color="gray" italic>
          Select an item in the tree to inspect details.
        </Text>
      </Box>
    );
  }

  if (selectedNode.type === 'agent') {
    const agent = selectedNode.info as AgentPaneInfo;
    const lastEv = agent.lastEvent;
    const ageStr = lastEv ? formatAge(lastEv.ts) : 'N/A';

    return (
      <Box flexDirection="column" borderStyle="single" borderColor="cyan" flexGrow={1} paddingX={1}>
        <Box justifyContent="space-between">
          <Box gap={1}>
            <Text color="cyan" bold>
              🤖 AGENT PANEL
            </Text>
            <Text color="white" bold>
              {agent.name || 'Unnamed Agent'}
            </Text>
            <Text color="gray">({agent.agentName})</Text>
          </Box>
          <StateBadge state={agent.state} />
        </Box>

        <Box flexDirection="column" marginTop={1} gap={0}>
          <Box gap={2}>
            <Text color="yellow">Pane ID:</Text>
            <Text color="white">{agent.id}</Text>
            <Text color="yellow">Label:</Text>
            <Text color="white">{agent.label || 'N/A'}</Text>
            <Text color="yellow">Task:</Text>
            <Text color="magenta">{agent.taskName}</Text>
            <Text color="yellow">Workspace:</Text>
            <Text color="brightYellow">{agent.workspaceName}</Text>
          </Box>

          <Box gap={2}>
            <Text color="yellow">CWD:</Text>
            <Text color="gray">{agent.cwd || 'N/A'}</Text>
          </Box>
        </Box>

        {/* Last Event Box */}
        <Box
          flexDirection="column"
          marginTop={1}
          borderStyle="classic"
          borderColor="gray"
          paddingX={1}
        >
          <Box justifyContent="space-between">
            <Text color="brightWhite" bold>
              ⚡ LAST EVENT LOG
            </Text>
            <Text color="gray">{ageStr}</Text>
          </Box>
          {lastEv ? (
            <Box flexDirection="column" marginTop={0}>
              <Box gap={2}>
                <Text color="yellow">Kind:</Text>
                <Text color="cyan">{lastEv.kind}</Text>
                <Text color="yellow">Timestamp:</Text>
                <Text color="gray">
                  {new Date(lastEv.ts * 1000).toLocaleTimeString()}
                </Text>
              </Box>
              {lastEv.detail ? (
                <Box gap={1} marginTop={0}>
                  <Text color="yellow">Note:</Text>
                  <Text color="white" bold>
                    "{lastEv.detail}"
                  </Text>
                </Box>
              ) : null}
            </Box>
          ) : (
            <Text color="gray" italic>
              No recorded events yet.
            </Text>
          )}
        </Box>
      </Box>
    );
  }

  if (selectedNode.type === 'workspace') {
    const ws = selectedNode.info as WorkspaceSessionInfo;
    return (
      <Box flexDirection="column" borderStyle="single" borderColor="yellow" flexGrow={1} paddingX={1}>
        <Box gap={1}>
          <Text color="yellow" bold>
            📁 WORKSPACE / SESSION
          </Text>
          <Text color="white" bold>
            {ws.name}
          </Text>
          <Text color="gray">({ws.id})</Text>
        </Box>

        <Box flexDirection="column" marginTop={1}>
          <Box gap={2}>
            <Text color="gray">CWD:</Text>
            <Text color="white">{ws.cwd}</Text>
          </Box>
          <Box gap={2}>
            <Text color="gray">Tasks / Windows:</Text>
            <Text color="magenta">{ws.tasks.length}</Text>
            <Text color="gray">Total Agents:</Text>
            <Text color="cyan">
              {ws.tasks.reduce((acc, t) => acc + t.panes.length, 0)}
            </Text>
          </Box>
        </Box>
      </Box>
    );
  }

  if (selectedNode.type === 'task') {
    const task = selectedNode.info as TaskWindowInfo;
    return (
      <Box flexDirection="column" borderStyle="single" borderColor="magenta" flexGrow={1} paddingX={1}>
        <Box gap={1}>
          <Text color="magenta" bold>
            📋 TASK / WINDOW
          </Text>
          <Text color="white" bold>
            {task.name}
          </Text>
          <Text color="gray">({task.id}, index {task.index})</Text>
        </Box>

        <Box flexDirection="column" marginTop={1}>
          <Box gap={2}>
            <Text color="gray">Workspace:</Text>
            <Text color="yellow">{task.workspaceName}</Text>
            <Text color="gray">Agents in Task:</Text>
            <Text color="cyan">{task.panes.length}</Text>
          </Box>
        </Box>
      </Box>
    );
  }

  return null;
};

function formatAge(ts: number): string {
  const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  return `${Math.floor(diffSec / 3600)}h ago`;
}
