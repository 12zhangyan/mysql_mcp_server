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
- 查询分页下推到 MySQL，避免客户端截断结果引发 Connector/Python `errno=-1`
- 内置结果行数和单元格长度限制，最大返回 1000 行
- 按源字段精确脱敏，支持别名、CTE 以及 JSON 内敏感键
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

生产环境建议固定经过审核的版本：

```bash
npx -y @yanzhang123/readonly-db-mcp@0.8.2
```

首次运行时，启动器会在用户缓存目录创建版本化虚拟环境，并安装包内 wheel 与带 SHA-256 锁定的 Python 依赖。完成后的环境按 wheel 和依赖锁指纹复用。可通过 `MYSQL_MCP_PYTHON` 指定 Python，通过 `MYSQL_MCP_NPM_CACHE_DIR` 修改缓存位置。

### 最小 MCP 配置

复制 [`mysql-connections.example.toml`](mysql-connections.example.toml) 为本地配置文件，把密码放入环境变量，并在 MCP 配置中使用绝对路径：

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.8.2"],
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

### 手动安装 Python 包

```bash
pip install mysql-mcp-server
```

Python 包仍适用于源码部署，但本分支的主要分发方式是上述 npm 包。

## 配置

### 推荐：命名连接

将 [`mysql-connections.example.toml`](mysql-connections.example.toml) 复制为已被 Git 忽略的 `mysql-connections.toml`，然后集中声明各环境：

```toml
default = "dev"

[connections.dev]
description = "本地开发环境"
host = "127.0.0.1"
port = 3306
user = "readonly_user"
password_env = "MYSQL_DEV_PASSWORD"
database = "app_dev"
allowed_databases = ["app_dev", "analytics_dev"]
query_timeout_ms = 30000
max_rows = 500
max_cell_length = 20000
result_format = "json"
mask_columns = ["password", "*password*", "*passwd*", "*pwd*", "*secret*", "token", "*token", "*_token", "token_*", "*tokenvalue*", "*accesstoken*", "*refreshtoken*", "*idtoken*", "*appsec*", "*api_key*", "*private_key*", "*ssn*", "*id_card*", "*phone*", "*mobile*", "*email*"]
pool_size = 3
audit_enabled = true

[connections.prod]
description = "生产只读副本"
host = "prod-read.example.internal"
user = "readonly_user"
password_env = "MYSQL_PROD_PASSWORD"
database = "app"
allowed_databases = ["app", "reporting"]
allowed_functions = [] # 仅填写已经审核、确定无副作用的 UDF/存储函数
query_timeout_ms = 15000
max_rows = 200
result_format = "json"
pool_size = 5
ssl_mode = "VERIFY_CA"
ssl_ca = "C:/certs/company-ca.pem"

# 企业审计配置
audit_enabled = true
audit_log_file = "C:/var/log/mysql-mcp/audit.jsonl"
audit_log_max_bytes = 10000000
audit_log_backup_count = 10
audit_hmac_key_env = "MYSQL_MCP_AUDIT_SIGNING_KEY"
audit_required_context = ["actor", "purpose", "ticket_id"]
audit_fail_closed = true
audit_fsync = true
```

通过环境变量提供配置路径和密钥：

```bash
MYSQL_PROFILES_FILE=C:/absolute/path/mysql-connections.toml
MYSQL_DEV_PASSWORD=...
MYSQL_PROD_PASSWORD=...
MYSQL_MCP_AUDIT_SIGNING_KEY=... # 由密钥管理系统提供，禁止提交
MYSQL_DEFAULT_CONNECTION=dev    # 可选，覆盖 TOML 中的 default
MYSQL_MAX_ROWS=500              # 可选，1-1000，默认 500
MYSQL_QUERY_TIMEOUT_MS=30000    # 整次调用超时，100-300000 毫秒
MYSQL_MAX_CELL_LENGTH=20000     # 超长单元格截断长度
MYSQL_RESULT_FORMAT=json        # json 或 csv
MYSQL_POOL_SIZE=0               # 旧单连接模式；命名配置默认 5
MYSQL_ALLOWED_DATABASES=app,reporting
MYSQL_ALLOWED_FUNCTIONS=        # 可选，经过审核的函数名
```

推荐使用 `password_env`。本地环境也支持 `password` 字段，但 `mysql-connections.toml` 已加入 `.gitignore`，因为它可能包含敏感信息。桌面 MCP 客户端的工作目录不固定，因此 `MYSQL_PROFILES_FILE` 必须使用绝对路径。

### 使用操作系统凭据库

用户级 MCP 配置可使用系统凭据库，让密码不出现在 MCP JSON 或 TOML 中：

```toml
[connections.dev]
host = "127.0.0.1"
user = "readonly_user"
credential_provider = "keyring"
credential_ref = "dev"
database = "app_dev"
allowed_databases = ["app_dev"]
```

使用遮罩输入在本机管理凭据：

```powershell
readonly-db-mcp credentials --profiles-file C:/absolute/path/mysql-connections.toml set dev
readonly-db-mcp credentials --profiles-file C:/absolute/path/mysql-connections.toml status dev
readonly-db-mcp credentials --profiles-file C:/absolute/path/mysql-connections.toml delete dev
```

Windows 下 `keyring` 使用 Windows 凭据管理器。无桌面的服务环境可设置 `credential_provider = "command"`，并通过 `credential_command = ["executable", "arg1"]` 调用经过批准的密钥管理 CLI。程序不会通过 shell 执行命令；stderr 会被丢弃，输出长度受限，且只接受一行非空 UTF-8 文本。失败信息不会包含密钥、命令参数或凭据引用。

### 逻辑环境路由

一个逻辑环境分布在多个物理实例时，可声明精确别名：

```toml
[routes.shared-test]
ubp = { connection = "test-ubp", database = "ubp" }
gts = { connection = "test-gts", database = "gts" }
eam = { connection = "test-eam", database = "eam" }
```

调用 `connection="shared-test", database="eam"` 时，只会解析到声明的目标。未知别名会明确失败，服务不会搜索或猜测其他环境。JSON 结果和审计记录会同时保留请求目标与实际目标。

每次调用都可以选择环境，无需重启：

```json
{
  "connection": "prod",
  "database": "app",
  "query": "SELECT COUNT(*) FROM orders",
  "audit_context": {
    "actor": "reporting-service",
    "purpose": "月末对账",
    "ticket_id": "FIN-2026-042"
  }
}
```

服务刻意不提供进程级“切换连接/数据库”工具。显式逐次选择可防止一个并发客户端悄悄改变另一个客户端的活动环境。

配置文件的大小或修改时间变化后会自动热加载。单个配置无效时，`validate_connections` 会报告该配置，但不会停用其他有效配置。密码环境变量只在使用对应连接时解析，因此生产凭据暂时不可用不会影响本地开发环境。

### 旧版单连接环境变量

未设置 `MYSQL_PROFILES_FILE` 时，仍可使用原有环境变量：

```bash
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=readonly_user
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=your_database
MYSQL_ALLOWED_DATABASES=your_database

MYSQL_SSL_MODE=REQUIRED          # DISABLED、REQUIRED、VERIFY_CA、VERIFY_IDENTITY
MYSQL_SSL_CA=                   # VERIFY_CA / VERIFY_IDENTITY 必填
MYSQL_CONNECT_TIMEOUT=10
MYSQL_QUERY_TIMEOUT_MS=30000
MYSQL_MAX_ROWS=500
MYSQL_MAX_CELL_LENGTH=20000
MYSQL_RESULT_FORMAT=json
MYSQL_POOL_SIZE=0
MYSQL_SQL_MODE=TRADITIONAL
MYSQL_CHARSET=utf8mb4
MYSQL_COLLATION=utf8mb4_unicode_ci
MYSQL_USE_PURE=false
MYSQL_RAISE_ON_WARNINGS=false
```

### Streamable HTTP 传输（推荐）

DeepSeek Harness 等新客户端可直接连接标准 MCP Streamable HTTP 端点：

```bash
MCP_TRANSPORT=streamable-http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_HTTP_PATH=/mcp
MCP_HTTP_ALLOWED_HOSTS=localhost:8000,127.0.0.1:8000
MCP_HTTP_BEARER_TOKEN=          # 可选，至少 32 个字符
MCP_HTTP_TRUST_PROXY_AUTH=false # 仅在认证反向代理是唯一入口时启用
MCP_HTTP_SESSION_IDLE_TIMEOUT_SECONDS=1800
```

默认端点是 `http://127.0.0.1:8000/mcp`。非回环地址只有在启用 Bearer
认证，或显式确认由认证反向代理保护时才允许启动。

### 旧版 SSE 传输

```bash
MCP_TRANSPORT=sse
MCP_SSE_HOST=127.0.0.1          # 默认仅监听回环地址
PORT=8000                       # MCP_SSE_PORT 的后备值
MCP_SSE_ALLOWED_HOSTS=          # 允许的 Host，逗号分隔
MCP_SSE_BEARER_TOKEN=           # 可选，至少 32 个字符
MCP_SSE_TRUST_PROXY_AUTH=false  # 仅在认证反向代理是唯一入口时启用
```

旧版端点为 `/sse` 和 `/messages/`，仅用于兼容现有客户端。部署细节见
[`ENTERPRISE_DEPLOYMENT.md`](ENTERPRISE_DEPLOYMENT.md)。

### SSH 隧道

```bash
MYSQL_SSH_ENABLE=false
MYSQL_SSH_HOST=
MYSQL_SSH_PORT=22
MYSQL_SSH_USER=
MYSQL_SSH_KEY_PATH=
MYSQL_SSH_REMOTE_HOST=localhost
MYSQL_SSH_REMOTE_PORT=3306
MYSQL_LOCAL_PORT=0              # 0 表示自动选择端口，隧道会复用
```

### `.env` 文件

服务启动时会通过 `python-dotenv` 从进程工作目录及父目录加载 `.env`：

```bash
cp .env.example .env
```

Claude Desktop、Codex 等宿主通常从自己的目录启动 MCP 服务，未必能找到项目内 `.env`。这类场景应将 `MYSQL_*` 配置放入 MCP 配置的 `env` 节点，或使用命名配置文件和凭据库。

### 多数据库模式

未设置 `MYSQL_DATABASE` 时，服务进入多数据库模式：

- `list_resources` 返回过滤系统库后的可访问数据库
- 数据工具通过 `database` 参数或 `database.table` 全限定名选择数据库
- `USE` 和多语句始终被拦截

## 可用工具

所有工具都声明 `readOnlyHint=true` 和 `destructiveHint=false`。

### `list_connections`

列出命名配置、默认数据库、策略限制和就绪状态，不返回主机、用户名、密码或 SSH 私钥路径。还会返回数据库路由索引和逻辑环境别名。

### `validate_connections`

强制重新加载配置，报告有效/无效配置及缺少的密码环境变量，不建立数据库连接。

### `check_connection`

通过只读健康检查返回 MySQL 版本、当前数据库、不可逆账号指纹、全局只读状态、授权数量、延迟和当前策略。原始用户名和 `SHOW GRANTS` 内容不会返回。

### `list_databases`

列出可访问的非系统数据库。

- 参数：`connection`（可选）
- 对逻辑连接返回声明的数据库别名，不建立数据库连接

### `list_tables`

列出指定数据库中的表和视图。

- 参数：`connection`、`database`、`table_name`/`table`（精确匹配）、`table_pattern`（支持 `*`/`?`）、`max_rows`、`offset`、`timeout_ms`、`result_format`，均可选
- 大库使用与 `execute_sql` 相同的 `truncated`/`next_offset` 分页契约

### `execute_sql`

执行且只执行一条只读语句。

- 必填参数：`query`
- 可选参数：`connection`、`database`、`max_rows`、`offset`、`timeout_ms`、`result_format`、`audit_context`
- 允许：`SELECT`、`WITH`、受数据库范围约束的 `SHOW`（包括 `SHOW CREATE TABLE/VIEW`）、`DESCRIBE`、`DESC`、`EXPLAIN`、`TABLE`
- 始终拦截：DML、DDL、`USE`、事务控制、锁、`SELECT ... INTO`、会话变量赋值、MySQL 可执行注释和多语句
- 函数策略：默认拦截无法识别的存储函数/UDF；只有 `allowed_functions` 中经过审核的确定性函数可放行
- 纵深防御：连接前先校验；随后在 `START TRANSACTION READ ONLY` 中执行并回滚
- 分页：对适用的 `SELECT`/CTE/UNION 下推 `LIMIT max_rows+1 OFFSET offset`，完整消费该有界结果后再回滚；JSON 结果返回 `truncated` 和 `next_offset`
- 跨库：使用 `database` 参数或 `database.table` 全限定名
- 超时/取消：统一限制完整操作、Connector socket 和 MySQL/MariaDB 语句时间；取消请求会关闭活动连接，无需 `KILL` 权限
- 格式：`json` 保留结构化数据和元信息；`csv` 使用标准引号规则，并明确表示 `NULL`
- 审计归因：`audit_context` 支持 `actor`、`purpose`、`ticket_id`，配置可要求调用前必须提供指定字段
- 临时故障恢复：Connector/Python 客户端侧 `errno=-1` 会重试一次；持续失败只返回安全的异常类型和生命周期阶段

### `query`

`execute_sql` 的兼容别名，使用相同的只读、白名单、脱敏、超时、分页和审计策略。不会恢复不安全的进程级 `use_connection` 或 `use_database`。

### `get_schema_info`

返回数据库结构详情，包括字段名、类型、可空性、默认值和注释。

- 参数：`table_name` 或兼容别名 `table`，以及 `connection`、`database`
- 可用 `database.table` 访问允许列表中的其他数据库
- 标识符只允许字母、数字、下划线、`$`，数据库与表之间允许一个点

### `get_table_sample`

返回具有代表性的少量样例数据。

- `table_name` 或兼容别名 `table` 必须提供一个
- 可选参数：`limit`（最大 100）、`offset`、`connection`、`database`、`timeout_ms`、`result_format`
- 采样会在 MySQL 中应用 `LIMIT limit+1 OFFSET offset` 并完整消费小结果集，不会为了返回几行数据而开启无界扫描结果

### `inspect_catalog`

返回 `tables`、`columns`、`indexes`、`constraints`、`foreign_keys` 或 `views` 的固定元数据投影。可通过 `table_name` 或 `table` 精确过滤。该工具不会开放任意 `information_schema` SQL。

## 可用提示词

| 提示词 | 参数 | 说明 |
| --- | --- | --- |
| `explore_database` | `connection`、`database`（可选） | 依次发现表、检查结构、采样并总结数据库 |
| `analyze_table` | `table_name`（必填）、`connection`、`database`（可选） | 深入分析指定表，支持 `database.table` |

这些提示词只编排只读的发现、结构检查和采样工具。

## MCP 客户端配置

### Claude Desktop

在 `claude_desktop_config.json` 中加入：

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.8.2"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "your_dev_password"
      }
    }
  }
}
```

### Visual Studio Code / Codex

在 `mcp.json` 或对应 MCP 配置中加入：

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp@0.8.2"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "your_dev_password"
      }
    }
  }
}
```

Windows 宿主如无法解析 `npx`，改用 `npx.cmd`。更多调用场景见 [`MCP_USECASES.md`](MCP_USECASES.md)。

### DeepSeek Harness

Harness 自带 `@deepseek-ai/dsh-mcp-client`。本地使用推荐挂载 npm 包的
STDIO 入口；在 Windows 上使用 `npx.cmd`：

```yaml
- insert:
    - id: mcp-mysql-readonly
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: mysql-readonly
        transport: stdio
        command: npx.cmd
        args: ['-y', '@yanzhang123/readonly-db-mcp@0.8.2']
        env:
          MYSQL_PROFILES_FILE: 'C:/absolute/path/mysql-connections.toml'
        toolCallTimeoutMs: 60000
        failOnStartupError: true
```

远程或集中部署时使用 Streamable HTTP。先按上文启动服务，然后在 Harness
配置中挂载端点；`MYSQL_MCP_AUTHORIZATION` 的值应为 `Bearer <token>`，并
从进程环境或密钥管理系统注入，不要提交到仓库：

```yaml
- insert:
    - id: mcp-mysql-readonly
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: mysql-readonly
        transport: streamable-http
        url: 'https://mcp.example.com/mcp'
        headers:
          Authorization: !!js process.env.MYSQL_MCP_AUTHORIZATION
        toolCallTimeoutMs: 60000
        failOnStartupError: true
```

## 开发与测试

```bash
git clone https://github.com/12zhangyan/mysql_mcp_server.git
cd mysql_mcp_server
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt
pytest
npm test
```

调试 MCP 协议交互时可使用 MCP Inspector，不建议直接把 Python 进程当作普通命令行程序交互。

## 安全设计

- **只读 SQL 闸门**：仅允许产生结果的只读语句；写入、DDL、事务、锁和有副作用的结构在连接前被拒绝
- **数据库侧约束**：所有查询以 `autocommit=false` 运行在只读事务中，正常结束统一回滚，取消时关闭连接触发服务端回滚
- **可写账号防护**：MCP 的只读行为不依赖账号授权；仍建议用专用 `SELECT` 账号作为独立防线
- **数据库白名单**：工具和资源 URI 均通过 MySQL AST 解析执行 `allowed_databases`，系统库默认禁止
- **受控元数据**：用户 SQL 默认不能直接查询 `information_schema`；使用 `list_tables`、`get_schema_info`、`inspect_catalog` 获取限定范围的元数据
- **凭据隔离**：密码可来自环境变量、操作系统凭据库或无 shell 的受控命令；发现接口、日志和错误均不返回凭据值或引用
- **资源限制**：整次调用、socket 和服务端语句均有超时；最多返回 1000 行，超长单元格会截断
- **精确脱敏**：依据表达式源字段脱敏，别名不能绕过规则，也不会连带遮罩无关列；JSON 中 `clientSecret`、`AppSec` 等敏感键递归脱敏
- **安全默认规则**：覆盖常见 password/PWD、mobile/phone、secret、凭据 token、API key、private key 命名；不会用宽泛的 `*token*` 误伤通道代码或 token 有效时长
- **企业审计**：UTC JSONL 事件包含事件 ID、MCP 请求 ID、操作、调用方归因、策略决定、目标库、无字面量查询指纹、耗时、结果大小和结果状态；不记录 SQL 原文和结果数据
- **两阶段审计**：失败关闭模式下，建立连接前先 fsync 写入 `started` 事件，随后记录终态事件
- **默认加密**：数据库 TLS 默认 `REQUIRED`；生产配置建议使用 `VERIFY_CA` 或 `VERIFY_IDENTITY`
- **解析器加固**：拒绝 MySQL `/*!...*/` 和 MariaDB `/*M!...*/` 可执行注释；未限定到允许数据库的全局 `SHOW` 被拦截
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
