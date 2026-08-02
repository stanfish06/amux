import React, { useMemo } from 'react';
import { Box, Text, useApp, useInput } from 'ink';
import { ActionHelp } from './components/ActionHelp.js';
import { AgentDetailPanel } from './components/AgentDetailPanel.js';
import { Header } from './components/Header.js';
import { MessageModal } from './components/MessageModal.js';
import { PaneTerminalPreview } from './components/PaneTerminalPreview.js';
import { SpawnModal } from './components/SpawnModal.js';
import { TreeNavigation } from './components/TreeNavigation.js';
import { TmuxService } from './tmux.js';
import { AgentPaneInfo, TreeNode } from './types.js';
import { useAmuxState } from './useAmuxState.js';

interface AppProps {
  socketName: string;
  pollIntervalMs: number;
}

export const App: React.FC<AppProps> = ({ socketName, pollIntervalMs }) => {
  const { exit } = useApp();
  const tmuxService = useMemo(() => new TmuxService(socketName), [socketName]);

  const {
    workspaces,
    selectedNodeId,
    selectedNode,
    expandedNodeIds,
    searchQuery,
    isSearching,
    activeModal,
    lastRefreshedAt,
    isLoading,
    error,
    capturedOutput,
    visibleNodes,
    statusMetrics,
    setSelectedNodeId,
    setSearchQuery,
    setIsSearching,
    setActiveModal,
    toggleExpand,
    moveSelection,
    refresh,
  } = useAmuxState(tmuxService, pollIntervalMs);

  // Keybindings handling
  useInput((input, key) => {
    if (activeModal !== 'none') {
      return;
    }

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

    if (input === 's') {
      setActiveModal('spawn');
      return;
    }

    if (input === 'm') {
      if (selectedNode && selectedNode.type === 'agent') {
        setActiveModal('message');
      }
      return;
    }

    if (input === 'k') {
      if (selectedNode) {
        handleKillNode(selectedNode);
      }
      return;
    }

    if (input === 'q') {
      exit();
      return;
    }
  });

  const handleKillNode = async (node: TreeNode) => {
    try {
      if (node.type === 'workspace') {
        await tmuxService.killWorkspace(node.workspaceName);
      } else if (node.type === 'task' && node.taskName) {
        await tmuxService.killTask(node.workspaceName, node.taskName);
      }
      refresh();
    } catch (err) {
      // ignore
    }
  };

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

      {/* Main Workspace Split View */}
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

      {/* Modals */}
      {activeModal === 'spawn' && (
        <Box position="absolute" marginTop={3} marginLeft={10}>
          <SpawnModal
            tmuxService={tmuxService}
            initialWorkspaceName={selectedNode?.workspaceName || 'myproj'}
            onClose={() => setActiveModal('none')}
            onSuccess={refresh}
          />
        </Box>
      )}

      {activeModal === 'message' && selectedNode?.type === 'agent' && (
        <Box position="absolute" marginTop={3} marginLeft={10}>
          <MessageModal
            tmuxService={tmuxService}
            agent={selectedNode.info as AgentPaneInfo}
            onClose={() => setActiveModal('none')}
            onSuccess={refresh}
          />
        </Box>
      )}
    </Box>
  );
};
