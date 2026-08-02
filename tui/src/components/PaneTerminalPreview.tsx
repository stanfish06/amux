import React from 'react';
import { Box, Text } from 'ink';

interface PaneTerminalPreviewProps {
  paneId?: string;
  outputLines: string[];
}

export const PaneTerminalPreview: React.FC<PaneTerminalPreviewProps> = ({
  paneId,
  outputLines,
}) => {
  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor="green"
      flexGrow={1}
      paddingX={1}
    >
      <Box justifyContent="space-between">
        <Text color="green" bold>
          🖥 PANE TERMINAL PREVIEW
        </Text>
        <Text color="gray">{paneId ? `[Target: ${paneId}]` : '[No Pane Selected]'}</Text>
      </Box>

      <Box flexDirection="column" marginTop={0}>
        {!paneId ? (
          <Text color="gray" italic>
            Select an agent pane to view live terminal screen capture.
          </Text>
        ) : outputLines.length === 0 ? (
          <Text color="gray" italic>
            (Empty terminal output buffer)
          </Text>
        ) : (
          outputLines.slice(-12).map((line, idx) => (
            <Text key={idx} color="brightWhite" wrap="truncate">
              {line || ' '}
            </Text>
          ))
        )}
      </Box>
    </Box>
  );
};
