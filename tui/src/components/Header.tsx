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
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
      {/* Top row: Brand Title & Socket Info */}
      <Box justifyContent="space-between">
        <Box gap={1}>
          <Text color="cyan" bold>
            ❖ AMUX
          </Text>
          <Text color="white" bold>
            REACTIVITY FRONTEND
          </Text>
          <Text color="gray">|</Text>
          <Text color="yellow">socket:</Text>
          <Text color="brightYellow" bold>
            {socketName}
          </Text>
        </Box>

        <Box gap={1}>
          {isLoading && (
            <Text color="cyan">
              <Spinner type="dots" /> Updating...
            </Text>
          )}
          <Text color="gray">Refreshed: {timeStr}</Text>
        </Box>
      </Box>

      {/* Middle row: Status Breakdown Metrics */}
      <Box justifyContent="space-between" marginTop={0}>
        <Box gap={2}>
          <Text color="gray">Agents: </Text>
          <Text color="white" bold>
            {metrics.totalPanes} total
          </Text>
          <Text color="green">
            ● {metrics.idle} idle
          </Text>
          <Text color="yellow">
            ⚡ {metrics.busy} busy
          </Text>

          {metrics.needsInput > 0 ? (
            <Text color="magenta" bold>
              ❓ {metrics.needsInput} needs-input
            </Text>
          ) : (
            <Text color="gray">
              ❓ {metrics.needsInput} needs-input
            </Text>
          )}

          {metrics.starting > 0 && (
            <Text color="cyan">
              🟦 {metrics.starting} starting
            </Text>
          )}

          {metrics.dead > 0 && (
            <Text color="red">
              💀 {metrics.dead} dead
            </Text>
          )}
        </Box>

        {isSearching || searchQuery ? (
          <Box gap={1}>
            <Text color="yellow">Filter:</Text>
            <Text color="white" backgroundColor="blue">
              {' '}{searchQuery || '(type to search)'}{' '}
            </Text>
          </Box>
        ) : null}
      </Box>
    </Box>
  );
};
