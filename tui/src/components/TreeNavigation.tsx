import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import { AgentState, TreeNode } from '../types.js';
import { BORDER_COLOR, STATE_STYLE } from '../theme.js';

const DEPTH = { workspace: 0, task: 1, agent: 2 } as const;

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
            <TreePanel width={width} padding={1}>
                <Text>
                    <Spinner type="dots" /> Loading amux workspaces ({socketName})...
                </Text>
            </TreePanel>
        );
    }

    if (nodes.length === 0) {
        return (
            <TreePanel width={width} padding={1}>
                <Text dimColor>No active workspaces found on socket: {socketName}.</Text>
            </TreePanel>
        );
    }

    return (
        <TreePanel width={width} paddingX={1}>
            <Box justifyContent="space-between">
                <Text bold>WORKSPACES &amp; AGENTS</Text>
                <Text dimColor>[{nodes.length}]</Text>
            </Box>

            <Box flexDirection="column">
                {nodes.map((node) => (
                    <TreeNodeRow
                        key={node.id}
                        node={node}
                        isSelected={node.id === selectedNodeId}
                        isExpanded={expandedNodeIds.has(node.id)}
                    />
                ))}
            </Box>
        </TreePanel>
    );
};

const TreePanel: React.FC<{
    width: number;
    padding?: number;
    paddingX?: number;
    children: React.ReactNode;
}> = ({ width, padding, paddingX, children }) => (
    <Box
        flexDirection="column"
        borderStyle="single"
        borderColor={BORDER_COLOR}
        width={width}
        padding={padding}
        paddingX={paddingX}
    >
        {children}
    </Box>
);

interface TreeNodeRowProps {
    node: TreeNode;
    isSelected: boolean;
    isExpanded: boolean;
}

const TreeNodeRow: React.FC<TreeNodeRowProps> = ({ node, isSelected, isExpanded }) => {
    const marker = node.type === 'agent' ? '' : `${isExpanded ? 'v' : '>'} `;

    return (
        <Box gap={1}>
            <Text bold>{isSelected ? '>' : ' '}</Text>
            <Text wrap="truncate">
                {'  '.repeat(DEPTH[node.type])}
                <Text bold={node.type !== 'agent' || isSelected}>
                    {marker}
                    {nodeTitle(node)}
                </Text>{' '}
                <NodeSummary node={node} />
            </Text>
        </Box>
    );
};

function nodeTitle(node: TreeNode): string {
    return node.type === 'agent' ? node.info.name || 'agent' : node.info.name;
}

const NodeSummary: React.FC<{ node: TreeNode }> = ({ node }) => {
    switch (node.type) {
        case 'workspace': {
            const { tasks } = node.info;
            const agentCount = tasks.reduce((sum, t) => sum + t.panes.length, 0);
            return (
                <Text dimColor>
                    ({agentCount} agents in {tasks.length} tasks)
                </Text>
            );
        }
        case 'task':
            return <Text dimColor>[{node.info.panes.length}]</Text>;
        case 'agent':
            return (
                <>
                    <Text dimColor>[{node.info.agentName}] </Text>
                    <StateBadge state={node.info.state} />
                    <Text dimColor> {node.info.id}</Text>
                </>
            );
    }
};

export const StateBadge: React.FC<{ state: AgentState }> = ({ state }) => {
    const { label, color, dim } = STATE_STYLE[state] ?? STATE_STYLE.unknown;
    return (
        <Text color={color} dimColor={dim} bold={!dim}>
            {label}
        </Text>
    );
};
