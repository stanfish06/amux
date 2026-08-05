import React from 'react';
import { Box, Text } from 'ink';
import { StateBadge } from './TreeNavigation.js';
import { AgentPaneInfo, TreeNode } from '../types.js';
import { BORDER_COLOR } from '../theme.js';

interface AgentDetailPanelProps {
    selectedNode: TreeNode | null;
}

export const AgentDetailPanel: React.FC<AgentDetailPanelProps> = ({ selectedNode }) => {
    if (!selectedNode) {
        return (
            <DetailPanel padding={1}>
                <Text dimColor>Select an item in the tree to inspect details.</Text>
            </DetailPanel>
        );
    }

    switch (selectedNode.type) {
        case 'agent':
            return <AgentDetail agent={selectedNode.info} />;

        case 'workspace': {
            const ws = selectedNode.info;
            const agentCount = ws.tasks.reduce((sum, t) => sum + t.panes.length, 0);
            return (
                <DetailPanel paddingX={1}>
                    <PanelTitle heading="WORKSPACE / SESSION" name={ws.name} note={ws.id} />
                    <Box flexDirection="column" marginTop={1}>
                        <Box gap={2}>
                            <Text dimColor>CWD:</Text>
                            <Text>{ws.cwd}</Text>
                        </Box>
                        <Box gap={2}>
                            <Text dimColor>Tasks / Windows:</Text>
                            <Text>{ws.tasks.length}</Text>
                            <Text dimColor>Total Agents:</Text>
                            <Text>{agentCount}</Text>
                        </Box>
                    </Box>
                </DetailPanel>
            );
        }

        case 'task': {
            const task = selectedNode.info;
            return (
                <DetailPanel paddingX={1}>
                    <PanelTitle
                        heading="TASK / WINDOW"
                        name={task.name}
                        note={`${task.id}, index ${task.index}`}
                    />
                    <Box flexDirection="column" marginTop={1}>
                        <Box gap={2}>
                            <Text dimColor>Workspace:</Text>
                            <Text>{task.workspaceName}</Text>
                            <Text dimColor>Agents in Task:</Text>
                            <Text>{task.panes.length}</Text>
                        </Box>
                    </Box>
                </DetailPanel>
            );
        }
    }
};

const AgentDetail: React.FC<{ agent: AgentPaneInfo }> = ({ agent }) => {
    const lastEvent = agent.lastEvent;

    return (
        <DetailPanel paddingX={1}>
            <Box justifyContent="space-between">
                <PanelTitle heading="AGENT" name={agent.name || 'Unnamed Agent'} note={agent.agentName} />
                <StateBadge state={agent.state} />
            </Box>

            <Box flexDirection="column" marginTop={1}>
                <Box gap={2}>
                    <Text dimColor>Pane ID:</Text>
                    <Text>{agent.id}</Text>
                    <Text dimColor>Label:</Text>
                    <Text>{agent.label || 'N/A'}</Text>
                    <Text dimColor>Task:</Text>
                    <Text>{agent.taskName}</Text>
                    <Text dimColor>Workspace:</Text>
                    <Text>{agent.workspaceName}</Text>
                </Box>
                <Box gap={2}>
                    <Text dimColor>CWD:</Text>
                    <Text>{agent.cwd || 'N/A'}</Text>
                </Box>
                {/* Only for a sandboxed agent. `amux event state --json` omits
                    these keys entirely for host agents, so a host agent's panel
                    is unchanged rather than gaining empty rows. */}
                {agent.runtime && agent.runtime !== 'host' ? (
                    <Box gap={2}>
                        <Text dimColor>Runtime:</Text>
                        <Text>{agent.runtime}</Text>
                        {agent.runtimeStatus ? (
                            <>
                                <Text dimColor>Status:</Text>
                                <Text>{agent.runtimeStatus}</Text>
                            </>
                        ) : null}
                        {agent.sandboxName ? (
                            <>
                                <Text dimColor>Sandbox:</Text>
                                <Text>{agent.sandboxName}</Text>
                            </>
                        ) : null}
                    </Box>
                ) : null}
            </Box>

            <Box
                flexDirection="column"
                marginTop={1}
                borderStyle="single"
                borderColor={BORDER_COLOR}
                paddingX={1}
            >
                <Box justifyContent="space-between">
                    <Text bold>LAST EVENT</Text>
                    <Text dimColor>{lastEvent ? formatAge(lastEvent.ts) : 'N/A'}</Text>
                </Box>

                {lastEvent ? (
                    <Box flexDirection="column">
                        <Box gap={2}>
                            <Text dimColor>Kind:</Text>
                            <Text>{lastEvent.kind}</Text>
                            <Text dimColor>Timestamp:</Text>
                            <Text>{new Date(lastEvent.ts * 1000).toLocaleTimeString()}</Text>
                        </Box>
                        {lastEvent.detail ? (
                            <Box gap={1}>
                                <Text dimColor>Note:</Text>
                                <Text wrap="truncate">"{lastEvent.detail}"</Text>
                            </Box>
                        ) : null}
                    </Box>
                ) : (
                    <Text dimColor>No recorded events yet.</Text>
                )}
            </Box>
        </DetailPanel>
    );
};

const DetailPanel: React.FC<{
    padding?: number;
    paddingX?: number;
    children: React.ReactNode;
}> = ({ padding, paddingX, children }) => (
    <Box
        flexDirection="column"
        borderStyle="single"
        borderColor={BORDER_COLOR}
        flexGrow={1}
        padding={padding}
        paddingX={paddingX}
    >
        {children}
    </Box>
);

const PanelTitle: React.FC<{ heading: string; name: string; note: string }> = ({
    heading,
    name,
    note,
}) => (
    <Box gap={1}>
        <Text bold>{heading}</Text>
        <Text bold>{name}</Text>
        <Text dimColor>({note})</Text>
    </Box>
);

function formatAge(ts: number): string {
    const diffSec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    return `${Math.floor(diffSec / 3600)}h ago`;
}
