import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';

interface HeaderProps {
  socketName: string;
  metrics: {
    totalPanes: number;
    starting: number;
    busy: number;
    idle: number;
    needsInput: number;
    dead: number;
  };
  isLoading: boolean;
  lastRefreshedAt: Date | null;
  searchQuery: string;
  isSearching: boolean;
}

export const Header: React.FC<HeaderProps> = ({
  socketName,
  metrics,
  isLoading,
  lastRefreshedAt,
  searchQuery,
  isSearching,
}) => {
  const timeStr = lastRefreshedAt
    ? lastRefreshedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    : '--:--:--';

  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1} width="100%">
      {/* Line 1: Title & Socket Info */}
      <Box justifyContent="space-between" width="100%">
        <Box gap={1}>
          <Text color="cyan" bold>❖ AMUX FRONTEND</Text>
          <Text color="gray">|</Text>
          <Text color="yellow">socket:</Text>
          <Text color="brightYellow" bold>{socketName}</Text>
        </Box>

        <Box gap={1}>
          {isLoading ? (
            <Text color="cyan">
              <Spinner type="dots" /> Updating...
            </Text>
          ) : (
            <Text color="gray">Refreshed: {timeStr}</Text>
          )}
        </Box>
      </Box>

      {/* Line 2: Status Breakdown Metrics */}
      <Box justifyContent="space-between" width="100%" marginTop={0}>
        <Text wrap="truncate">
          <Text color="gray">Agents: </Text>
          <Text color="white" bold>{metrics.totalPanes} total</Text>
          <Text color="gray">  |  </Text>
          <Text color="green">● {metrics.idle} idle</Text>
          <Text color="gray">  </Text>
          <Text color="yellow">⚡ {metrics.busy} busy</Text>
          <Text color="gray">  </Text>
          <Text color={metrics.needsInput > 0 ? 'magenta' : 'gray'} bold={metrics.needsInput > 0}>
            ❓ {metrics.needsInput} input
          </Text>
          {metrics.starting > 0 && (
            <>
              <Text color="gray">  </Text>
              <Text color="cyan">🟦 {metrics.starting} start</Text>
            </>
          )}
          {metrics.dead > 0 && (
            <>
              <Text color="gray">  </Text>
              <Text color="red">💀 {metrics.dead} dead</Text>
            </>
          )}
        </Text>

        {isSearching || searchQuery ? (
          <Box gap={1}>
            <Text color="yellow">Filter:</Text>
            <Text color="white" backgroundColor="blue">
              {' '}{searchQuery || '(search)'}{' '}
            </Text>
          </Box>
        ) : null}
      </Box>
    </Box>
  );
};
