"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const requested = process.argv[2];
if (!requested || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(requested)) {
  process.stderr.write(
    "Usage: npm run release:version -- <semver>, for example 0.7.1\n",
  );
  process.exit(1);
}

const packagePath = path.join(root, "package.json");
const manifest = JSON.parse(fs.readFileSync(packagePath, "utf8"));
manifest.version = requested;
fs.writeFileSync(packagePath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

function replaceVersion(relativePath, pattern, replacement) {
  const target = path.join(root, relativePath);
  const original = fs.readFileSync(target, "utf8");
  const updated = original.replace(pattern, replacement);
  if (updated === original) {
    throw new Error(`Could not update version in ${relativePath}`);
  }
  fs.writeFileSync(target, updated, "utf8");
}

replaceVersion(
  "pyproject.toml",
  /^version = "[^"]+"/m,
  `version = "${requested}"`,
);
replaceVersion(
  path.join("src", "mysql_mcp_server", "__init__.py"),
  /__version__ = "[^"]+"/,
  `__version__ = "${requested}"`,
);

const npmCommand =
  process.platform === "win32" ? process.env.ComSpec || "cmd.exe" : "npm";
const npmPrefix =
  process.platform === "win32" ? ["/d", "/s", "/c", "npm.cmd"] : [];
const npmLock = spawnSync(
  npmCommand,
  [...npmPrefix, "install", "--package-lock-only", "--ignore-scripts"],
  { cwd: root, stdio: "inherit", windowsHide: true },
);
if (npmLock.status !== 0) {
  throw new Error("Failed to update package-lock.json");
}

const uvCommand = process.platform === "win32" ? "uv.exe" : "uv";
const uvLock = spawnSync(uvCommand, ["lock"], {
  cwd: root,
  stdio: "inherit",
  windowsHide: true,
});
if (uvLock.error && uvLock.error.code === "ENOENT") {
  process.stderr.write("uv is unavailable; run `uv lock` before committing.\n");
} else if (uvLock.status !== 0) {
  throw new Error("Failed to update uv.lock");
}

process.stdout.write(
  `Version set to ${requested}. Update CHANGELOG.md, run tests, commit, and push main.\n`,
);
