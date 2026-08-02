import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import { AgentState, StatusMetrics } from '../types.js';
import { BORDER_COLOR, STATE_STYLE } from '../theme.js';

const METRICS: Array<{ state: AgentState; label: string; always: boolean }> = [
    { state: 'idle', label: 'idle', always: true },
    { state: 'busy', label: 'busy', always: true },
    { state: 'needs-input', label: 'input', always: true },
    { state: 'starting', label: 'start', always: false },
    { state: 'dead', label: 'dead', always: false },
];

interface HeaderProps {
    socketName: string;
    metrics: StatusMetrics;
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
        ? lastRefreshedAt.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        })
        : '--:--:--';

    return (
        <Box flexDirection="column" borderStyle="single" borderColor={BORDER_COLOR} paddingX={1}>
            <Box justifyContent="space-between">
                <Box gap={1}>
                    <Text bold>AMUX</Text>
                    <Text dimColor>|</Text>
                    <Text dimColor>socket:</Text>
                    <Text bold>{socketName}</Text>
                </Box>

                {isLoading ? (
                    <Text>
                        <Spinner type="dots" /> Updating...
                    </Text>
                ) : (
                    <Text dimColor>Refreshed: {timeStr}</Text>
                )}
            </Box>

            <Box justifyContent="space-between">
                <Text wrap="truncate">
                    <Text dimColor>Agents: </Text>
                    <Text bold>{metrics.total} total</Text>
                    {METRICS.filter((m) => m.always || metrics[m.state] > 0).map((m) => (
                        <Text key={m.state} color={STATE_STYLE[m.state].color}>
                            {'   '}
                            {metrics[m.state]} {m.label}
                        </Text>
                    ))}
                </Text>

                {isSearching || searchQuery ? (
                    <Box gap={1}>
                        <Text dimColor>Filter:</Text>
                        <Text bold inverse>
                            {' '}
                            {searchQuery || '(search)'}{' '}
                        </Text>
                    </Box>
                ) : null}
            </Box>
        </Box>
    );
};
