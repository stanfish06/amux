import React from 'react';
import { Box, Text } from 'ink';
import { BORDER_COLOR } from '../theme.js';

interface PaneTerminalPreviewProps {
    paneId?: string;
    outputLines: string[];
}

export const PaneTerminalPreview: React.FC<PaneTerminalPreviewProps> = ({
    paneId,
    outputLines,
}) => (
    <Box
        flexDirection="column"
        borderStyle="single"
        borderColor={BORDER_COLOR}
        flexGrow={1}
        paddingX={1}
    >
        <Box justifyContent="space-between">
            <Text bold>PANE PREVIEW</Text>
            <Text dimColor>{paneId ? `[${paneId}]` : '[no pane selected]'}</Text>
        </Box>

        <Box flexDirection="column">
            <PreviewBody paneId={paneId} outputLines={outputLines} />
        </Box>
    </Box>
);

const PreviewBody: React.FC<PaneTerminalPreviewProps> = ({ paneId, outputLines }) => {
    if (!paneId) {
        return <Text dimColor>Select an agent pane to view its terminal.</Text>;
    }
    if (outputLines.length === 0) {
        return <Text dimColor>(empty terminal output buffer)</Text>;
    }
    return (
        <>
            {outputLines.map((line, idx) => (
                <Text key={idx} wrap="truncate">
                    {line || ' '}
                </Text>
            ))}
        </>
    );
};
