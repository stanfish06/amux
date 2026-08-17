import { AgentState } from './types.js';

export const BORDER_COLOR = 'white';

const RED = '#D70A53';
const YELLOW = '#FFF244';
const GREEN = '#66EB66';
const BLUE = '#9CCFD8';

export const STATE_STYLE: Record<AgentState, { label: string; color: string; dim: boolean }> = {
  starting: { label: '[START]', color: BLUE, dim: false },
  busy: { label: '[BUSY ]', color: YELLOW, dim: false },
  idle: { label: '[IDLE ]', color: GREEN, dim: false },
  'needs-input': { label: '[INPUT]', color: RED, dim: false },
  // Seven characters like every other label: the Ink boxes align on that width,
  // and an emoji is not reliably one cell wide so it breaks the borders.
  stopped: { label: '[STOP ]', color: 'cyan', dim: true },
  dead: { label: '[DEAD ]', color: RED, dim: true },
  unknown: { label: '[ ??? ]', color: 'white', dim: true },
};
