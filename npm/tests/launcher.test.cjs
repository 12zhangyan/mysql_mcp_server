"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const manifest = require("../../package.json");
const {
  cacheBase,
  dependencyLock,
  detectPython,
  pythonCandidates,
  virtualenvPython,
} = require("../lib/launcher.cjs");

test("manifest exposes the expected public CLI", () => {
  assert.equal(manifest.name, "@yanzhang123/readonly-db-mcp");
  assert.equal(
    manifest.bin["readonly-db-mcp"],
    "npm/bin/mysql-mcp-server-readonly.cjs",
  );
  assert.equal(
    manifest.bin["mysql-mcp-server-readonly"],
    "npm/bin/mysql-mcp-server-readonly.cjs",
  );
  assert.equal(manifest.publishConfig.access, "public");
  assert.match(manifest.repository.url, /12zhangyan\/mysql_mcp_server/);
});

test("hashed Python dependency lock is packaged", () => {
  const lock = fs.readFileSync(dependencyLock(), "utf8");
  assert.match(lock, /--hash=sha256:/);
  assert.match(lock, /mcp==/);
  assert.match(lock, /mysql-connector-python==/);
});

test("configured Python takes precedence", () => {
  assert.deepEqual(pythonCandidates({ MYSQL_MCP_PYTHON: "/opt/python" }, "linux"), [
    ["/opt/python", []],
  ]);
});

test("Python detection rejects versions older than 3.11", () => {
  assert.throws(
    () =>
      detectPython({
        candidates: [["old-python", []]],
        spawnSync: () => ({ status: 0, stdout: "3.10\n" }),
      }),
    /Python 3\.11/,
  );
});

test("cache and virtualenv paths are platform-specific", () => {
  assert.equal(
    cacheBase(
      {
        MYSQL_MCP_NPM_CACHE_DIR: path.join("tmp", "mysql-cache"),
      },
      "linux",
    ),
    path.resolve("tmp", "mysql-cache"),
  );
  assert.match(virtualenvPython("C:\\cache\\venv", "win32"), /python\.exe$/);
  assert.match(virtualenvPython("/cache/venv", "linux"), /bin[\\/]python$/);
});

test("launcher version does not initialize Python", () => {
  const result = spawnSync(
    process.execPath,
    ["npm/bin/mysql-mcp-server-readonly.cjs", "--launcher-version"],
    { cwd: path.resolve(__dirname, "..", ".."), encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), manifest.version);
});
