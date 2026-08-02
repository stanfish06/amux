import React, { useState } from 'react';
import { Box, Text, useInput } from 'ink';
import TextInput from 'ink-text-input';
import { TmuxService } from '../tmux.js';

interface SpawnModalProps {
  tmuxService: TmuxService;
  initialWorkspaceName?: string;
  onClose: () => void;
  onSuccess: () => void;
}

export const SpawnModal: React.FC<SpawnModalProps> = ({
  tmuxService,
  initialWorkspaceName = 'proj',
  onClose,
  onSuccess,
}) => {
  const [mode, setMode] = useState<'workspace' | 'task'>('workspace');
  const [workspaceName, setWorkspaceName] = useState(initialWorkspaceName);
  const [taskName, setTaskName] = useState('task0');
  const [agentSpec, setAgentSpec] = useState('claude:2');
  const [activeField, setActiveField] = useState<number>(0);
  const [statusMsg, setStatusMsg] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);

  const fields = mode === 'workspace'
    ? ['mode', 'workspaceName', 'taskName', 'agentSpec']
    : ['mode', 'workspaceName', 'taskName', 'agentSpec'];

  useInput((input, key) => {
    if (key.escape) {
      onClose();
      return;
    }

    if (key.tab || (key.downArrow && activeField < fields.length - 1)) {
      setActiveField((prev) => (prev + 1) % fields.length);
      return;
    }
    if (key.upArrow && activeField > 0) {
      setActiveField((prev) => Math.max(0, prev - 1));
      return;
    }

    if (fields[activeField] === 'mode' && (input === ' ' || key.rightArrow || key.leftArrow)) {
      setMode((prev) => (prev === 'workspace' ? 'task' : 'workspace'));
      return;
    }

    if (key.return) {
      if (activeField < fields.length - 1) {
        setActiveField((prev) => prev + 1);
      } else {
        handleSubmit();
      }
    }
  });

  const handleSubmit = async () => {
    if (!workspaceName.trim()) {
      setStatusMsg('Workspace name cannot be empty');
      return;
    }
    setIsSubmitting(true);
    setStatusMsg('Spawning...');

    try {
      const agents = agentSpec.trim() ? agentSpec.trim().split(/\s+/) : ['claude'];
      if (mode === 'workspace') {
        await tmuxService.spawnWorkspace(workspaceName.trim(), undefined, taskName.trim(), undefined, undefined, agents);
      } else {
        await tmuxService.spawnTask(workspaceName.trim(), taskName.trim(), undefined, undefined, undefined, agents);
      }
      onSuccess();
      onClose();
    } catch (err: any) {
      setIsSubmitting(false);
      setStatusMsg(`Error: ${err.message || err}`);
    }
  };

  return (
    <Box
      flexDirection="column"
      borderStyle="double"
      borderColor="green"
      padding={1}
      width={60}
    >
      <Text color="green" bold>
        🚀 SPAWN AGENT SPACE / TASK
      </Text>
      <Text color="gray">Press Tab / Up / Down to navigate, Enter to submit, Esc to cancel</Text>

      <Box flexDirection="column" marginTop={1} gap={1}>
        {/* Field 0: Mode */}
        <Box gap={2}>
          <Text color={activeField === 0 ? 'cyan' : 'gray'}>Target Mode:</Text>
          <Text color="yellow" bold>
            [{mode.toUpperCase()}]
          </Text>
          <Text color="gray">(Space/Arrows to toggle)</Text>
        </Box>

        {/* Field 1: Workspace Name */}
        <Box gap={2}>
          <Text color={activeField === 1 ? 'cyan' : 'gray'}>Workspace Name:</Text>
          {activeField === 1 ? (
            <TextInput value={workspaceName} onChange={setWorkspaceName} />
          ) : (
            <Text color="white">{workspaceName}</Text>
          )}
        </Box>

        {/* Field 2: Task Name */}
        <Box gap={2}>
          <Text color={activeField === 2 ? 'cyan' : 'gray'}>Task / Window Name:</Text>
          {activeField === 2 ? (
            <TextInput value={taskName} onChange={setTaskName} />
          ) : (
            <Text color="white">{taskName}</Text>
          )}
        </Box>

        {/* Field 3: Agent Spec */}
        <Box gap={2}>
          <Text color={activeField === 3 ? 'cyan' : 'gray'}>Agent Spec (e.g. claude:2 codex):</Text>
          {activeField === 3 ? (
            <TextInput value={agentSpec} onChange={setAgentSpec} />
          ) : (
            <Text color="white">{agentSpec}</Text>
          )}
        </Box>
      </Box>

      {statusMsg ? (
        <Box marginTop={1}>
          <Text color={isSubmitting ? 'yellow' : 'red'}>{statusMsg}</Text>
        </Box>
      ) : null}
    </Box>
  );
};
