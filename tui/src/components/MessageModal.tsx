import React, { useState } from 'react';
import { Box, Text, useInput } from 'ink';
import TextInput from 'ink-text-input';
import { TmuxService } from '../tmux.js';
import { AgentPaneInfo } from '../types.js';

interface MessageModalProps {
  tmuxService: TmuxService;
  agent: AgentPaneInfo;
  onClose: () => void;
  onSuccess: () => void;
}

export const MessageModal: React.FC<MessageModalProps> = ({
  tmuxService,
  agent,
  onClose,
  onSuccess,
}) => {
  const [message, setMessage] = useState('');
  const [statusMsg, setStatusMsg] = useState('');
  const [isSending, setIsSending] = useState(false);

  useInput((_input, key) => {
    if (key.escape) {
      onClose();
    }
  });

  const handleSubmit = async () => {
    if (!message.trim()) return;
    setIsSending(true);
    setStatusMsg('Sending message...');

    try {
      await tmuxService.sendKeysToPane(agent.id, message.trim());
      onSuccess();
      onClose();
    } catch (err: any) {
      setIsSending(false);
      setStatusMsg(`Failed: ${err.message || err}`);
    }
  };

  return (
    <Box
      flexDirection="column"
      borderStyle="double"
      borderColor="magenta"
      padding={1}
      width={60}
    >
      <Text color="magenta" bold>
        💬 SEND PROMPT / INPUT TO AGENT
      </Text>
      <Box gap={1}>
        <Text color="gray">Target:</Text>
        <Text color="cyan" bold>{agent.name}</Text>
        <Text color="gray">[{agent.agentName}]</Text>
        <Text color="yellow">{agent.id}</Text>
      </Box>

      <Box marginTop={1} gap={1}>
        <Text color="magenta" bold>&gt;</Text>
        <TextInput
          value={message}
          onChange={setMessage}
          onSubmit={handleSubmit}
          placeholder="Type instruction or response to agent..."
        />
      </Box>

      {statusMsg ? (
        <Box marginTop={1}>
          <Text color={isSending ? 'yellow' : 'red'}>{statusMsg}</Text>
        </Box>
      ) : (
        <Box marginTop={1}>
          <Text color="gray">Press Enter to send, Esc to cancel</Text>
        </Box>
      )}
    </Box>
  );
};
