import React from 'react';
import { Box, Text } from 'ink';

export const ActionHelp: React.FC = () => {
  return (
    <Box
      flexDirection="row"
      borderStyle="single"
      borderColor="gray"
      paddingX={1}
      justifyContent="space-around"
    >
      <Box gap={1}>
        <Text color="cyan" bold>[j/k / ↑↓]</Text>
        <Text color="gray">Navigate</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[Space/Enter]</Text>
        <Text color="gray">Expand/Select</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[a]</Text>
        <Text color="gray">Attach Session</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[s]</Text>
        <Text color="gray">Spawn Space/Task</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[m]</Text>
        <Text color="gray">Message Pane</Text>
      </Box>
      <Box gap={1}>
        <Text color="red" bold>[k]</Text>
        <Text color="gray">Kill Node</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[/]</Text>
        <Text color="gray">Filter</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[r]</Text>
        <Text color="gray">Refresh</Text>
      </Box>
      <Box gap={1}>
        <Text color="cyan" bold>[q]</Text>
        <Text color="gray">Quit</Text>
      </Box>
    </Box>
  );
};
