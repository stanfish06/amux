import React from 'react';
import { Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import { AgentState, StatusMetrics } from '../types.js';
import { BORDER_COLOR, STATE_STYLE } from '../theme.js';

// `satisfies` rather than a type annotation: annotating this as
// Array<{state: AgentState, ...}> widens `state` back to AgentState, which makes
// any exhaustiveness check derived from it vacuously true. With `as const
// satisfies`, the shape is still checked but the literal states survive, so the
// guard below can see which ones are actually listed.
const METRICS = [
    { state: 'idle', label: 'idle', always: true },
    { state: 'busy', label: 'busy', always: true },
    { state: 'needs-input', label: 'input', always: true },
    { state: 'starting', label: 'start', always: false },
    // `always: false` matches starting and dead: shown only when non-zero,
    // which is right for a state most workspaces will not have.
    { state: 'stopped', label: 'stopped', always: false },
    { state: 'dead', label: 'dead', always: false },
] as const satisfies ReadonlyArray<{
    state: AgentState;
    label: string;
    always: boolean;
}>;

// This is an array, so a missing entry is valid TypeScript and simply hides a
// bucket -- while `metrics.total` still counts that pane. The header would then
// show a total its own per-state numbers do not sum to: an agent inside the
// total, in no bucket, invisible. So the omission is made a compile error.
//
// The `Exclude<...>` is written out twice on purpose. DO NOT factor it into a
// type alias to tidy it up: with an alias the error reads "Type true is not
// assignable to type _Missing", which names nothing. Inline, it reads "...not
// assignable to type "stopped"" -- the states you forgot, enumerated in the
// diagnostic. Whoever adds a seventh state months from now will have none of
// this context, so the error message has to do the teaching.
//
// `'unknown'` is excluded inside the expression rather than by a wrapper type,
// for the same reason: it is this side's fallback for a pane it cannot classify,
// never reported by Python, and has never had a header bucket.
const _every: Exclude<
    AgentState,
    'unknown' | (typeof METRICS)[number]['state']
> extends never
    ? true
    : Exclude<AgentState, 'unknown' | (typeof METRICS)[number]['state']> = true;
void _every;

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
