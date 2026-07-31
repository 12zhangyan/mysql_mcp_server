# Release Process

Python and npm use one synchronized version. Run:

```bash
npm run release:version -- 0.7.1
```

This updates `package.json`, `package-lock.json`, `pyproject.toml`,
`src/mysql_mcp_server/__init__.py`, and `uv.lock`. Then update `CHANGELOG.md`,
run the full test suite, and commit the version change:

```bash
git add package.json package-lock.json pyproject.toml uv.lock \
  src/mysql_mcp_server/__init__.py CHANGELOG.md
git commit -m "release: v0.7.1"
git push origin main
```

A push to `main` that changes `package.json` runs
`.github/workflows/publish-npm.yml`. The workflow verifies synchronized
versions, builds the Python wheel, embeds it in the npm tarball, tests both
launchers, and publishes only when that exact npm version does not already
exist. A `v0.7.1` tag can also trigger the same workflow.

## Initial npm Publication

The package name is `mysql-mcp-server-readonly`. The first publication must be
performed by an authenticated npm owner. Use staged publishing so that the
package can be reviewed in npm before its public release:

```bash
npm login
npx --yes npm@12 stage publish
```

Then open npmjs.com, go to **Staged Packages**, inspect the staged artifact, and
approve it with 2FA. No npm token belongs in this repository.

## Automatic npm Authentication

Preferred: configure an npm Trusted Publisher for the published package:

- Provider: GitHub Actions
- GitHub organization/user: `12zhangyan`
- Repository: `mysql_mcp_server`
- Workflow filename: `publish-npm.yml`
- Environment: `npm`
- Allowed action: `npm publish`

The workflow has `id-token: write`, uses a GitHub-hosted runner, and publishes
with provenance.

Token fallback: create a granular npm automation token with the minimum package
scope, then store it as the GitHub Actions repository secret `NPM_TOKEN`.
Never put the token in `.npmrc`, workflow YAML, source files, issues, or logs.
