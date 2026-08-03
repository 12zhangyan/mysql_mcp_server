#!/usr/bin/env node

"use strict";

const { main } = require("../lib/launcher.cjs");

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`mysql-mcp-server-readonly: ${message}\n`);
  process.exitCode = 1;
});
