import React from 'react';
import { Box, Text } from 'ink';

export const ActionHelp: React.FC = () => {
  return (
    <Box
      flexDirection="row"
      borderStyle="single"
      borderColor="gray"
      paddingX={1}
      gap={2}
      width="100%"
      flexWrap="nowrap"
    >
      <Text wrap="truncate">
        <Text color="cyan" bold>[j/k]</Text> <Text color="gray">Navigate</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[Space]</Text> <Text color="gray">Expand</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[a]</Text> <Text color="gray">Attach</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[s]</Text> <Text color="gray">Spawn</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[m]</Text> <Text color="gray">Message</Text>
        <Text color="gray">  │  </Text>
        <Text color="red" bold>[k]</Text> <Text color="gray">Kill</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[/]</Text> <Text color="gray">Filter</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[r]</Text> <Text color="gray">Refresh</Text>
        <Text color="gray">  │  </Text>
        <Text color="cyan" bold>[q]</Text> <Text color="gray">Quit</Text>
      </Text>
    </Box>
  );
};
