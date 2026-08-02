import React from 'react';
import { Box, Text } from 'ink';
import { BORDER_COLOR } from '../theme.js';

const KEYS: Array<[string, string]> = [
    ['j/k', 'Navigate'],
    ['Space/Enter', 'Expand/Collapse'],
    ['/', 'Filter'],
    ['r', 'Refresh'],
    ['q', 'Quit'],
];

export const ActionHelp: React.FC = () => (
    <Box borderStyle="single" borderColor={BORDER_COLOR} paddingX={1}>
        <Text wrap="truncate">
            {KEYS.map(([key, action], idx) => (
                <Text key={key}>
                    {idx > 0 ? <Text dimColor>{'   '}</Text> : null}
                    <Text bold>[{key}]</Text>
                    <Text dimColor> {action}</Text>
                </Text>
            ))}
        </Text>
    </Box>
);
