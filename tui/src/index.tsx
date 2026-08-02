#!/usr/bin/env node
import { render } from 'ink';
import { Command, InvalidArgumentError } from 'commander';
import { App } from './App.js';

const DEFAULTS = { interval: 1500, width: 120, treeWidth: 44 };

function positiveInt(value: string): number {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed <= 0) {
        throw new InvalidArgumentError('expected a positive integer');
    }
    return parsed;
}

const program = new Command()
    .name('amux-tui')
    .description('Read-only monitoring dashboard for amux agents')
    .option('-L, --socket-name <socket>', 'tmux socket name', 'amux-root')
    .option('-i, --interval <ms>', 'poll interval in milliseconds', positiveInt, DEFAULTS.interval)
    .option('-W, --width <cols>', 'total dashboard width in columns', positiveInt, DEFAULTS.width)
    .option('-T, --tree-width <cols>', 'workspace tree width in columns', positiveInt, DEFAULTS.treeWidth)
    .parse(process.argv);

const { socketName, interval, width, treeWidth } = program.opts<{
    socketName: string;
    interval: number;
    width: number;
    treeWidth: number;
}>();

if (treeWidth >= width) {
    program.error(`--tree-width (${treeWidth}) must be less than --width (${width})`);
}

render(
    <App
        socketName={socketName}
        pollIntervalMs={interval}
        width={width}
        treeWidth={treeWidth}
    />
);
