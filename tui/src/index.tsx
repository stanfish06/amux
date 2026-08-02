#!/usr/bin/env node
import React from 'react';
import { render } from 'ink';
import { Command } from 'commander';
import { App } from './App.js';

const program = new Command();

program
  .name('amux-tui')
  .description('Reactive TUI frontend for amux agent multiplexer')
  .option('-L, --socket-name <socket>', 'tmux socket name', 'amux-root')
  .option('-i, --interval <ms>', 'poll interval in milliseconds', '1500')
  .parse(process.argv);

const options = program.opts();
const socketName = options.socketName;
const pollIntervalMs = parseInt(options.interval, 10) || 1500;

render(<App socketName={socketName} pollIntervalMs={pollIntervalMs} />);
