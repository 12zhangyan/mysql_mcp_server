"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const manifest = require("../../package.json");
const packageRoot = path.resolve(__dirname, "..", "..");

function pythonCandidates(environment = process.env, platform = process.platform) {
  if (environment.MYSQL_MCP_PYTHON) {
    return [[environment.MYSQL_MCP_PYTHON, []]];
  }
  if (platform === "win32") {
    return [
      ["py", ["-3.11"]],
      ["py", ["-3"]],
      ["python", []],
    ];
  }
  return [
    ["python3", []],
    ["python", []],
  ];
}

function detectPython(options = {}) {
  const candidates = options.candidates || pythonCandidates();
  const run = options.spawnSync || spawnSync;
  for (const [command, prefix] of candidates) {
    const result = run(
      command,
      [
        ...prefix,
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
      ],
      {
        encoding: "utf8",
        windowsHide: true,
      },
    );
    if (result.status !== 0) {
      continue;
    }
    const match = String(result.stdout).trim().match(/^(\d+)\.(\d+)$/);
    if (match && (Number(match[1]) > 3 || Number(match[2]) >= 11)) {
      return { command, prefix };
    }
  }
  throw new Error(
    "Python 3.11 or newer is required. Install Python or set MYSQL_MCP_PYTHON.",
  );
}

function cacheBase(environment = process.env, platform = process.platform) {
  if (environment.MYSQL_MCP_NPM_CACHE_DIR) {
    return path.resolve(environment.MYSQL_MCP_NPM_CACHE_DIR);
  }
  if (platform === "win32") {
    return path.join(
      environment.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"),
      "mysql-mcp-server-readonly",
    );
  }
  if (platform === "darwin") {
    return path.join(os.homedir(), "Library", "Caches", "mysql-mcp-server-readonly");
  }
  return path.join(
    environment.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"),
    "mysql-mcp-server-readonly",
  );
}

function bundledWheel(root = packageRoot) {
  const vendor = path.join(root, "npm", "vendor");
  if (!fs.existsSync(vendor)) {
    throw new Error("The npm package is missing its bundled Python wheel.");
  }
  const wheels = fs
    .readdirSync(vendor)
    .filter((name) => name.endsWith(".whl"))
    .sort();
  if (wheels.length !== 1) {
    throw new Error(
      `Expected exactly one bundled Python wheel, found ${wheels.length}.`,
    );
  }
  return path.join(vendor, wheels[0]);
}

function dependencyLock(root = packageRoot) {
  const lock = path.join(root, "npm", "requirements.lock");
  if (!fs.existsSync(lock)) {
    throw new Error("The npm package is missing its hashed Python dependency lock.");
  }
  return lock;
}

function runtimeFingerprint(wheel, lock) {
  const digest = crypto.createHash("sha256");
  digest.update(fs.readFileSync(wheel));
  digest.update(fs.readFileSync(lock));
  return digest.digest("hex");
}

function runtimeIsReady(marker, runtimePython, fingerprint) {
  if (!fs.existsSync(marker) || !fs.existsSync(runtimePython)) {
    return false;
  }
  try {
    const state = JSON.parse(fs.readFileSync(marker, "utf8"));
    return state.runtimeFingerprint === fingerprint;
  } catch {
    return false;
  }
}

function virtualenvPython(virtualenv, platform = process.platform) {
  return platform === "win32"
    ? path.join(virtualenv, "Scripts", "python.exe")
    : path.join(virtualenv, "bin", "python");
}

function runChecked(command, args, label) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`${label} failed: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${label} failed with exit code ${result.status}`);
  }
}

function sleep(milliseconds) {
  Atomics.wait(
    new Int32Array(new SharedArrayBuffer(4)),
    0,
    0,
    milliseconds,
  );
}

function acquireInstallLock(lockPath, markerPath) {
  const deadline = Date.now() + 180_000;
  while (true) {
    try {
      return fs.openSync(lockPath, "wx");
    } catch (error) {
      if (!error || error.code !== "EEXIST") {
        throw error;
      }
      if (fs.existsSync(markerPath)) {
        return null;
      }
      const age = Date.now() - fs.statSync(lockPath).mtimeMs;
      if (age > 600_000) {
        fs.rmSync(lockPath, { force: true });
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error("Timed out waiting for another runtime installation.");
      }
      sleep(250);
    }
  }
}

function ensureRuntime() {
  const wheel = bundledWheel();
  const lockfile = dependencyLock();
  const fingerprint = runtimeFingerprint(wheel, lockfile);
  const runtimeRoot = path.join(cacheBase(), manifest.version);
  const virtualenv = path.join(runtimeRoot, "venv");
  const marker = path.join(runtimeRoot, "ready.json");
  const lock = path.join(runtimeRoot, "install.lock");
  const runtimePython = virtualenvPython(virtualenv);

  if (runtimeIsReady(marker, runtimePython, fingerprint)) {
    return runtimePython;
  }

  fs.mkdirSync(runtimeRoot, { recursive: true });
  const lockHandle = acquireInstallLock(lock, marker);
  if (lockHandle === null) {
    return runtimePython;
  }
  try {
    if (runtimeIsReady(marker, runtimePython, fingerprint)) {
      return runtimePython;
    }
    const python = detectPython();
    if (!fs.existsSync(runtimePython)) {
      runChecked(
        python.command,
        [...python.prefix, "-m", "venv", virtualenv],
        "Python virtual environment creation",
      );
    }
    runChecked(
      runtimePython,
      [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--require-hashes",
        "--only-binary=:all:",
        "--upgrade",
        "-r",
        lockfile,
      ],
      "Locked Python dependency installation",
    );
    runChecked(
      runtimePython,
      [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-deps",
        "--upgrade",
        wheel,
      ],
      "Bundled Python package installation",
    );
    fs.writeFileSync(
      marker,
      `${JSON.stringify({
        packageVersion: manifest.version,
        wheel: path.basename(wheel),
        runtimeFingerprint: fingerprint,
      })}\n`,
      "utf8",
    );
    return runtimePython;
  } finally {
    fs.closeSync(lockHandle);
    fs.rmSync(lock, { force: true });
  }
}

async function main(argv = process.argv.slice(2)) {
  if (argv.includes("--launcher-version")) {
    process.stdout.write(`${manifest.version}\n`);
    return;
  }

  const python = ensureRuntime();
  const child = spawn(python, ["-m", "mysql_mcp_server", ...argv], {
    env: process.env,
    stdio: "inherit",
    windowsHide: true,
  });

  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => {
      if (!child.killed) {
        child.kill(signal);
      }
    });
  }

  await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      if (signal) {
        reject(new Error(`Python MCP exited after signal ${signal}`));
      } else {
        process.exitCode = code === null ? 1 : code;
        resolve();
      }
    });
  });
}

module.exports = {
  bundledWheel,
  cacheBase,
  dependencyLock,
  detectPython,
  ensureRuntime,
  main,
  pythonCandidates,
  virtualenvPython,
};
