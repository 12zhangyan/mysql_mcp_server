"use strict";

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const manifest = require(path.join(root, "package.json"));
const dist = path.join(root, "dist");
const vendor = path.join(root, "npm", "vendor");
const expectedPrefix = `mysql_mcp_server-${manifest.version}-`;

if (!fs.existsSync(dist)) {
  throw new Error("dist/ is missing; build the Python wheel before npm packaging.");
}
const wheels = fs
  .readdirSync(dist)
  .filter(
    (name) => name.startsWith(expectedPrefix) && name.endsWith(".whl"),
  );
if (wheels.length !== 1) {
  throw new Error(
    `Expected one ${expectedPrefix}*.whl in dist/, found ${wheels.length}.`,
  );
}

fs.mkdirSync(vendor, { recursive: true });
for (const name of fs.readdirSync(vendor)) {
  if (name.endsWith(".whl")) {
    fs.rmSync(path.join(vendor, name), { force: true });
  }
}
fs.copyFileSync(path.join(dist, wheels[0]), path.join(vendor, wheels[0]));
process.stdout.write(`Staged npm/vendor/${wheels[0]}\n`);
