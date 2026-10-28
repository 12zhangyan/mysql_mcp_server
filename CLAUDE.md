# CLAUDE.md

面向 Claude Code 和其他 Agent 的仓库指引。用户文档见 [README.md](README.md) 和 [docs/](docs/)。

## 项目概述

严格只读的 MySQL MCP 服务：Python 实现（`src/mysql_mcp_server/`），通过 npm 包 `@yanzhang123/readonly-db-mcp` 发布，npm 启动器（`npm/`）会安装包内的 wheel。Python 与 npm 使用同一个版本号。

## 目录

| 路径 | 内容 |
|---|---|
| `src/mysql_mcp_server/sql_guard.py` | 只读 SQL 闸门：关键字、函数、系统库校验 |
| `src/mysql_mcp_server/server.py`、`tool_specs.py` | 工具注册、分发和查询执行；工具定义（参数、描述）在 `tool_specs.py` |
| `src/mysql_mcp_server/config.py` | 命名连接、环境变量和凭据解析 |
| `src/mysql_mcp_server/results.py` | 结果格式、分页、脱敏 |
| `src/mysql_mcp_server/runtime.py`、`audit.py`、`transport.py` | 并发准入、审计、HTTP/SSE 传输 |
| `tests/` | pytest；`tests/snapshots/list_tools.json` 固定对外工具列表 |
| `npm/` | 启动器和打包测试 |
| `docs/` | 用户文档；`docs/<日期>/` 是历史开发方案，`docs/INDEX.md` 由 `project-html/build.js` 生成，不要手改 |

## 常用命令

```bash
# 环境（Windows PowerShell 激活：.\.venv\Scripts\Activate.ps1）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest                      # 全部单元测试；带 MySQL 的验收测试默认跳过
mypy src
npm test                    # 版本一致性检查加启动器测试

# 打包安装测试：先构建当前版本的 wheel（dist/ 已被 Git 忽略）
pip install build && python -m build --wheel   # 或 uv build --wheel
npm run test:install
```

真实库验收测试（只读）：设置 `MYSQL_MCP_LOCAL_TESTS=1`、`MYSQL_USER`、`MYSQL_PASSWORD` 后运行 `pytest tests/test_local_readonly.py -q`，固定连接 `127.0.0.1:3306`。CI 用 MySQL 服务容器运行它们。凭据只放进程环境，不写入文件。

## 规则

- **不放宽只读边界**：`BLOCKED_FUNCTIONS`、系统库拦截、带库名前缀的函数调用需要显式放行等规则变更，必须先在方案或 PR 中说明理由，并补测试。
- **工具列表是契约**：改动工具名称、参数或描述会改变 `tests/snapshots/list_tools.json`，快照变化必须是有意的，并在 CHANGELOG 记录。
- **sqlglot 版本跨度大**：`pyproject.toml` 允许 26.x 到 30.x，同一函数在不同版本里可能被建模或不被建模；SQL 校验相关改动要在最低版本和最新版本上都测，CI 的 `minimum`、`locked`、`latest` 三个组合就是为此。
- **只面向 MySQL**：不做数据库适配层，不做 MariaDB 实库验证。
- 不提交密码、连接串、审计 HMAC 密钥、真实 `mysql-connections.toml` 或含敏感信息的 MCP 客户端配置。

## 提交与发版

- 从 `main` 建分支（例如 `claude/<主题>`），提交后开 PR，CI 通过后用合并提交合并，不直接推 `main`。
- 发版按 [RELEASING.md](RELEASING.md)：`npm run release:version -- X.Y.Z`，更新 CHANGELOG，重新生成 `npm/requirements.lock`，跑 `npm test` 与 `npm run test:install`，单独开 `release: vX.Y.Z` 的 PR。合并到 `main` 且 `package.json` 有变化时，`publish-npm.yml` 自动发布 npm。
- 提交信息沿用现有风格：`fix:`、`docs:`、`perf:`、`release:` 等前缀加一句英文摘要。
