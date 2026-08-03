"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const manifest = require(path.join(root, "package.json"));
const packageLock = require(path.join(root, "package-lock.json"));
const pyproject = fs.readFileSync(path.join(root, "pyproject.toml"), "utf8");
const init = fs.readFileSync(
  path.join(root, "src", "mysql_mcp_server", "__init__.py"),
  "utf8",
);

const pyprojectVersion = pyproject.match(
  /^\s*version\s*=\s*"([^"]+)"/m,
)?.[1];
const fallbackVersion = init.match(/__version__\s*=\s*"([^"]+)"/)?.[1];

const versions = {
  packageJson: manifest.version,
  packageLock: packageLock.version,
  packageLockRoot: packageLock.packages?.[""]?.version,
  pyproject: pyprojectVersion,
  pythonFallback: fallbackVersion,
};
const unique = new Set(Object.values(versions));
if (unique.size !== 1 || unique.has(undefined)) {
  process.stderr.write(`Version mismatch: ${JSON.stringify(versions)}\n`);
  process.exit(1);
}
process.stdout.write(`Version verified: ${manifest.version}\n`);
