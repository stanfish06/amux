import React, { useMemo } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import { ActionHelp } from './components/ActionHelp.js';
import { AgentDetailPanel } from './components/AgentDetailPanel.js';
import { Header } from './components/Header.js';
import { PaneTerminalPreview } from './components/PaneTerminalPreview.js';
import { TreeNavigation } from './components/TreeNavigation.js';
import { TmuxService } from './tmux.js';
import { useAmuxState } from './useAmuxState.js';
import { BORDER_COLOR } from './theme.js';

interface AppProps {
    socketName: string;
    pollIntervalMs: number;
    width: number;
    treeWidth: number;
}

export const App: React.FC<AppProps> = ({ socketName, pollIntervalMs, width, treeWidth }) => {
    const { exit } = useApp();
    const tmuxService = useMemo(() => new TmuxService(socketName), [socketName]);
    const detailWidth = width - treeWidth;

    const {
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
    } = useAmuxState(tmuxService, pollIntervalMs);

    useInput((input, key) => {
        if (isSearching) {
            if (key.escape || key.return) {
                setIsSearching(false);
            } else if (key.backspace || key.delete) {
                setSearchQuery((prev) => prev.slice(0, -1));
            } else if (input && !key.ctrl && !key.meta) {
                setSearchQuery((prev) => prev + input);
            }
            return;
        }

        if (key.upArrow || input === 'k') moveSelection('up');
        else if (key.downArrow || input === 'j') moveSelection('down');
        else if (key.return || input === ' ') {
            if (selectedNode && selectedNode.type !== 'agent') toggleExpand(selectedNode.id);
        } else if (input === '/') setIsSearching(true);
        else if (input === 'r') refresh();
        else if (input === 'q') exit();
    });

    return (
        <Box flexDirection="column" width={width}>
            <Header
                socketName={socketName}
                metrics={statusMetrics}
                isLoading={isLoading}
                lastRefreshedAt={lastRefreshedAt}
                searchQuery={searchQuery}
                isSearching={isSearching}
            />

            {error ? (
                <Box borderStyle="single" borderColor={BORDER_COLOR} padding={1} width="100%">
                    <Text bold>ERROR: {error}</Text>
                </Box>
            ) : (
                <Box flexDirection="row" width="100%">
                    <TreeNavigation
                        nodes={visibleNodes}
                        selectedNodeId={selectedNodeId}
                        expandedNodeIds={expandedNodeIds}
                        width={treeWidth}
                        isLoading={isLoading}
                        lastRefreshedAt={lastRefreshedAt}
                        socketName={socketName}
                    />

                    <Box flexDirection="column" width={detailWidth}>
                        <AgentDetailPanel selectedNode={selectedNode} />
                        <PaneTerminalPreview
                            paneId={selectedNode?.type === 'agent' ? selectedNode.info.id : undefined}
                            outputLines={capturedOutput}
                        />
                    </Box>
                </Box>
            )}

            <ActionHelp />
        </Box>
    );
};
