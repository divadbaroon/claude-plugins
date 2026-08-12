#!/usr/bin/env node
'use strict';

const { run } = require('../lib/cli');

run().then(
  (code) => { process.exitCode = code; },
  (error) => {
    process.stderr.write(`human-compact: ${error.message}\n`);
    process.exitCode = 1;
  },
);
