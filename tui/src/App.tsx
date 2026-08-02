import React, { useMemo } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import { ActionHelp } from './components/ActionHelp.js';
import { AgentDetailPanel } from './components/AgentDetailPanel.js';
import { Header } from './components/Header.js';
import { PaneTerminalPreview } from './components/PaneTerminalPreview.js';
import { TreeNavigation } from './components/TreeNavigation.js';
import { TmuxService } from './tmux.js';
import { useAmuxState } from './useAmuxState.js';

interface AppProps {
  socketName: string;
  pollIntervalMs: number;
}

export const App: React.FC<AppProps> = ({ socketName, pollIntervalMs }) => {
  const { exit } = useApp();
  const tmuxService = useMemo(() => new TmuxService(socketName), [socketName]);

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
        return;
      }
      if (key.backspace || key.delete) {
        setSearchQuery((prev) => prev.slice(0, -1));
        return;
      }
      if (input && !key.ctrl && !key.meta) {
        setSearchQuery((prev) => prev + input);
        return;
      }
      return;
    }

    if (key.upArrow || input === 'k') {
      moveSelection('up');
      return;
    }
    if (key.downArrow || input === 'j') {
      moveSelection('down');
      return;
    }

    if (key.return || input === ' ') {
      if (selectedNode) {
        if (selectedNode.type === 'workspace' || selectedNode.type === 'task') {
          toggleExpand(selectedNode.id);
        }
      }
      return;
    }

    if (input === '/') {
      setIsSearching(true);
      return;
    }

    if (input === 'r') {
      refresh();
      return;
    }

    if (input === 'q') {
      exit();
      return;
    }
  });

  return (
    <Box flexDirection="column" width={120}>
      {/* Header Bar */}
      <Header
        socketName={socketName}
        metrics={statusMetrics}
        isLoading={isLoading}
        lastRefreshedAt={lastRefreshedAt}
        searchQuery={searchQuery}
        isSearching={isSearching}
      />

      {/* Main Split View */}
      {error ? (
        <Box borderStyle="single" borderColor="red" padding={1} width="100%">
          <Text color="red" bold>
            ⚠️ {error}
          </Text>
        </Box>
      ) : (
        <Box flexDirection="row" width="100%" gap={0}>
          {/* Left Column: Tree Navigation */}
          <TreeNavigation
            nodes={visibleNodes}
            selectedNodeId={selectedNodeId}
            expandedNodeIds={expandedNodeIds}
            width={44}
            isLoading={isLoading}
            lastRefreshedAt={lastRefreshedAt}
            socketName={socketName}
          />

          {/* Right Column: Detail Inspector & Terminal Capture */}
          <Box flexDirection="column" width={76}>
            <AgentDetailPanel selectedNode={selectedNode} />
            <PaneTerminalPreview
              paneId={selectedNode?.type === 'agent' ? selectedNode.paneId : undefined}
              outputLines={capturedOutput}
            />
          </Box>
        </Box>
      )}

      {/* Action Footer Bar */}
      <ActionHelp />
    </Box>
  );
};
