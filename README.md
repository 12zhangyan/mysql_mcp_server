[![Tests](https://github.com/12zhangyan/mysql_mcp_server/actions/workflows/test.yml/badge.svg)](https://github.com/12zhangyan/mysql_mcp_server/actions)
[![npm version](https://img.shields.io/npm/v/%40yanzhang123%2Freadonly-db-mcp)](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp)
[![npm provenance](https://img.shields.io/badge/npm-provenance-blue)](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp)

# MySQL MCP Server

一个可审计、严格只读的 MySQL Model Context Protocol（MCP）服务。单个进程即可安全访问多个 MySQL 环境和数据库。

本分支推荐通过 npm 包 [`@yanzhang123/readonly-db-mcp`](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp) 使用。npm 包内置匹配版本的 Python wheel 和跨平台启动器，MCP 客户端无需检出仓库或单独安装 Python 包。

> [!NOTE]
> 服务支持标准输入输出（STDIO）、标准 Streamable HTTP 和旧版 SSE 传输。远程或自托管场景推荐使用 Streamable HTTP，并应按本文配置认证和网络边界。

> [!IMPORTANT]
> 只读约束不依赖 MySQL 账号权限。即使账号拥有 `INSERT`、`UPDATE`、`DELETE` 或 DDL 权限，SQL 闸门也只接受经过审查的只读语句，并在只读事务中执行后统一回滚。生产环境仍强烈建议使用仅授予 `SELECT` 的数据库账号，形成独立的纵深防御。

## 功能特性

- 使用命名连接配置管理开发、测试、预发、生产等多个环境
- 每次工具调用都显式选择 `connection` 和 `database`，避免并发客户端相互污染状态
- 支持逻辑数据库到物理连接的精确路由，不会隐式跨环境切换
- 严格只读 SQL：失败即拒绝的语法校验、MySQL 只读事务、无条件回滚
- 可发现连接、数据库、表、字段、索引、约束和样例数据
- 整库表/列元数据可分页；`inspect_catalog` / `get_schema_info` / `dump_schema` 返回 `truncated` 与 `next_offset`，禁止把单页当成全集
- 查询分页下推到 MySQL，避免客户端截断结果引发 Connector/Python `errno=-1`
- 内置结果行数和单元格长度限制，最大返回 1000 行
- 结果默认不脱敏；按连接配置需要隐藏的字段，支持别名、CTE 和 JSON 内键
- 支持 SSL/TLS、SSH 隧道、Streamable HTTP 和旧版 SSE 传输
- 密码可保存在环境变量、操作系统凭据库或受控的密钥命令中
- 保留旧版单连接 `MYSQL_*` 环境变量和 `query` 工具别名

## 安装

### npm / npx（推荐）

环境要求：Node.js 18+、npm 9+，并确保 Python 3.11+ 可从 `PATH` 找到。

直接运行最新版本：

```bash
npx -y @yanzhang123/readonly-db-mcp
```

生产环境建议固定经过审核的版本，在包名后加 `@<版本>`，版本号见 [npm 页面](https://www.npmjs.com/package/@yanzhang123/readonly-db-mcp?activeTab=versions)。

首次运行时，启动器会在用户缓存目录创建版本化虚拟环境，并安装包内 wheel 与带 SHA-256 锁定的 Python 依赖。完成后的环境按 wheel 和依赖锁指纹复用。可通过 `MYSQL_MCP_PYTHON` 指定 Python，通过 `MYSQL_MCP_NPM_CACHE_DIR` 修改缓存位置。

### Claude Code 一行安装

先复制 [`mysql-connections.example.toml`](mysql-connections.example.toml) 为本地配置文件，再把配置文件的绝对路径和密码环境变量交给 Claude Code：

```bash
claude mcp add mysql-readonly \
  -e MYSQL_PROFILES_FILE=/absolute/path/mysql-connections.toml \
  -e MYSQL_DEV_PASSWORD=由密钥存储提供 \
  -- npx -y @yanzhang123/readonly-db-mcp
```

Windows 上无法启动 `npx` 时改用 `npx.cmd`。其他客户端的配置见 [客户端配置](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/CONFIGURATION.md#mcp-客户端配置)。

### 最小 MCP 配置

复制 [`mysql-connections.example.toml`](mysql-connections.example.toml) 为本地配置文件，把密码放入环境变量，并在 MCP 配置中使用绝对路径：

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "由客户端密钥存储提供"
      }
    }
  }
}
```

部分 Windows MCP 客户端无法正确解析 npm 命令垫片，此时将 `command` 改为 `npx.cmd`。不要提交真实连接配置、密码、审计 HMAC 密钥、连接串或含有敏感信息的 MCP 客户端配置。

服务启动后，建议依次调用 `validate_connections` 和 `check_connection`，确认配置有效、网络可达、账号权限符合预期。`check_connection` 只返回不可逆账号指纹和权限分类，不会暴露用户名或原始授权语句。

### 从源码安装 Python 包

本项目只通过上述 npm 包发布，不发布到 PyPI。PyPI 上名为 `mysql-mcp-server` / `mysql_mcp_server` 的包属于上游原项目，不包含本分支的只读、审计和脱敏实现，请勿用 `pip install mysql-mcp-server` 安装。源码部署时从本仓库安装：

```bash
git clone https://github.com/12zhangyan/mysql_mcp_server.git
cd mysql_mcp_server
pip install ".[http]"   # 只用 STDIO 时可省略 [http]
```

## 文档

- [配置](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/CONFIGURATION.md)：命名连接、结果脱敏、凭据库、逻辑环境路由、传输、SSH 隧道和各客户端配置
- [工具与提示词](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/TOOLS.md)：12 个工具的参数、响应预算、并发控制和可纠正的错误码
- [常见问题与排障](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/TROUBLESHOOTING.md)
- [开发与测试](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/TESTING.md)
- [企业部署](ENTERPRISE_DEPLOYMENT.md) · [安全说明](SECURITY.md) · [发布流程](https://github.com/12zhangyan/mysql_mcp_server/blob/main/RELEASING.md) · [使用场景](https://github.com/12zhangyan/mysql_mcp_server/blob/main/MCP_USECASES.md)

## 可用工具

所有工具都声明 `readOnlyHint=true` 和 `destructiveHint=false`。参数和行为详见 [工具与提示词](https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/TOOLS.md)。

| 工具 | 用途 |
| --- | --- |
| `list_connections` / `validate_connections` / `check_connection` | 列出命名连接、重载并校验配置、只读健康检查 |
| `list_databases` / `list_tables` | 列出可访问的数据库、表和视图 |
| `execute_sql` / `query` | 执行单条只读语句（`query` 是兼容别名） |
| `explain_sql` | 对 `SELECT` / `WITH` 生成执行计划 |
| `get_schema_info` / `inspect_catalog` / `dump_schema` | 字段、索引、约束、视图等元数据，支持分页和整库导出 |
| `get_table_sample` | 返回少量样例数据 |

## 安全设计

- **只读 SQL 闸门**：仅允许产生结果的只读语句；写入、DDL、事务、锁和有副作用的结构在连接前被拒绝
- **数据库侧约束**：所有查询以 `autocommit=false` 运行在只读事务中，正常结束统一回滚，取消时关闭连接触发服务端回滚
- **可写账号防护**：MCP 的只读行为不依赖账号授权；仍建议用专用 `SELECT` 账号作为独立防线
- **数据库白名单**：工具和资源 URI 均通过 MySQL AST 解析执行 `allowed_databases`，系统库默认禁止
- **受控元数据**：用户 SQL 默认不能直接查询 `information_schema`；使用 `list_tables`、`get_schema_info`、`inspect_catalog` 获取限定范围的元数据
- **凭据隔离**：密码可来自环境变量、操作系统凭据库或无 shell 的受控命令；发现接口、日志和错误均不返回凭据值或引用
- **资源限制**：整次调用、socket 和服务端语句均有超时；最多返回 1000 行，超长单元格会截断
- **可配置脱敏**：默认全部放开；通过 `mask_columns` / `MYSQL_MASK_COLUMNS` 指定字段后才脱敏，追踪别名和表达式来源，并递归处理 JSON 内匹配的键
- **企业审计**：UTC JSONL 事件包含事件 ID、MCP 请求 ID、操作、调用方归因、策略决定、目标库、无字面量查询指纹、耗时、结果大小和结果状态；不记录 SQL 原文和结果数据
- **两阶段审计**：失败关闭模式下，建立连接前先 fsync 写入 `started` 事件，随后记录终态事件
- **默认加密**：数据库 TLS 默认 `REQUIRED`；生产配置建议使用 `VERIFY_CA` 或 `VERIFY_IDENTITY`
- **解析器加固**：拒绝 MySQL `/*!...*/` 和 MariaDB `/*M!...*/` 可执行注释；未限定到允许数据库的全局 `SHOW` 被拦截；每次查询显式设置已校验的 `sql_mode`，不接受改变字符串边界的 `ANSI_QUOTES`、`NO_BACKSLASH_ESCAPES` 等模式
- **诊断隐私**：连接诊断只公开不可逆账号指纹和权限摘要，不返回用户名、授权原文、配置路径或数据库错误原文
- **SSE 安全边界**：默认仅绑定回环地址；非回环部署必须配置 Bearer 认证或由唯一入口的认证反向代理保护

详细安全和企业部署说明：

- [`SECURITY.md`](SECURITY.md)
- [`ENTERPRISE_DEPLOYMENT.md`](ENTERPRISE_DEPLOYMENT.md)

## 安全最佳实践

1. 使用仅授予 `SELECT` 的专用 MySQL 用户，不使用 root 或管理员账号
2. 通过 `allowed_databases` 将每个连接限制到实际需要的数据库
3. 对生产连接启用证书校验，并限制网络访问来源
4. 对重要环境要求完整的 `audit_context`，审计 HMAC 密钥由密钥管理系统提供
5. 定期复核脱敏规则、审计事件、依赖版本和账号授权

## 发布

Python 与 npm 使用同一版本号。维护者应按 [`RELEASING.md`](RELEASING.md) 完成同步版本、测试、打包和发布。

## 参与贡献

1. Fork 仓库
2. 创建功能分支
3. 提交修改并运行相关测试
4. 推送分支并创建 Pull Request

提交安全问题前请先阅读 [`SECURITY.md`](SECURITY.md)。

## 许可证

本项目使用 MIT License，详见 [`LICENSE`](LICENSE)。
