"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const manifest = require("../../package.json");
const { virtualenvPython } = require("../lib/launcher.cjs");

function checked(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    windowsHide: true,
    ...options,
  });
  assert.equal(
    result.status,
    0,
    `${command} ${args.join(" ")} failed\n${result.error || ""}\n${
      result.stdout || ""
    }\n${result.stderr || ""}`,
  );
  return result;
}

function runNpm(args, options = {}) {
  return process.platform === "win32"
    ? checked(
        process.env.ComSpec || "cmd.exe",
        ["/d", "/s", "/c", "npm.cmd", ...args],
        options,
      )
    : checked("npm", args, options);
}

test(
  "packed npm artifact installs and bootstraps the bundled Python wheel",
  { timeout: 300_000 },
  () => {
    const root = path.resolve(__dirname, "..", "..");
    const temporary = fs.mkdtempSync(
      path.join(os.tmpdir(), "mysql-mcp-npm-install-"),
    );
    try {
      const packed = runNpm(["pack", "--pack-destination", temporary], {
        cwd: root,
      });
      const filename = packed.stdout.trim().split(/\r?\n/).at(-1);
      assert.match(filename, /\.tgz$/);
      const tarball = path.join(temporary, filename);
      runNpm(["install", "--prefix", temporary, "--ignore-scripts", tarball]);

      const launcher = path.join(
        temporary,
        "node_modules",
        manifest.name,
        "npm",
        "bin",
        "mysql-mcp-server-readonly.cjs",
      );
      const version = checked(process.execPath, [launcher, "--launcher-version"]);
      assert.equal(version.stdout.trim(), manifest.version);

      const runtimeCache = path.join(temporary, "runtime-cache");
      checked(process.execPath, [launcher], {
        input: "",
        timeout: 240_000,
        env: {
          ...process.env,
          MYSQL_MCP_NPM_CACHE_DIR: runtimeCache,
        },
      });

      const runtimePython = virtualenvPython(
        path.join(runtimeCache, manifest.version, "venv"),
      );
      assert.ok(fs.existsSync(runtimePython));
      const installed = checked(runtimePython, [
        "-c",
        "import mysql_mcp_server; print(mysql_mcp_server.__version__)",
      ]);
      assert.equal(installed.stdout.trim(), manifest.version);
    } finally {
      fs.rmSync(temporary, { recursive: true, force: true });
    }
  },
);
