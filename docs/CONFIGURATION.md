# 配置

> 返回 [README](../README.md)。以下各节从主 README 迁入，内容未改。

## 配置

### 结果脱敏配置

**默认不脱敏。** 代码不再内置敏感字段名单；省略配置或配置为空时，所有结果字段（包括密码、Token、联系方式及 JSON 内键）均按查询结果返回，`masked_columns` 为空。

命名连接通过 `mask_columns` 指定要隐藏的字段。配置属于各自连接，支持大小写不敏感的字段名和通配符：

```toml
[connections.dev]
# 其他连接参数见下方完整示例。
mask_columns = [] # 默认全部放开；省略此项效果相同。
# 如需脱敏，将上面一行改为自己的字段列表，例如：
# mask_columns = ["password", "access_token", "users.phone"]
```

旧单连接模式通过 `MYSQL_MASK_COLUMNS` 配置，以逗号分隔；未设置或设为空字符串均不脱敏。例如 `MYSQL_MASK_COLUMNS=password,access_token` 只按这两个规则处理。使用命名连接文件时，以各连接自己的 `mask_columns` 为准。

显式配置会把每个结果列追溯到底层表的字段：别名、表达式、函数与类型转换、窗口函数、标量子查询与关联子查询、派生表、CTE（含列清单）、`UNION`、`LATERAL`、`JSON_TABLE`、`TABLE` 语句和 `*` 都会继续追踪，并对 JSON 内匹配的键递归处理。按路径从 JSON 文档读取的值也会检查路径中的键：`JSON_EXTRACT`、`->`、`->>`、`JSON_VALUE` 和 `JSON_TABLE` 列的路径只要经过匹配不带点规则的键（例如 `$.contact.phone` 匹配 `phone`），读出的值就会隐藏；路径含通配符、递归 `**` 或不是常量时按敏感处理。派生表或 CTE 中名称匹配规则的列也视为敏感来源；只有最外层的别名不算。不带点的模式只匹配字段名，例如 `password`；带点的模式按限定路径匹配，例如 `users.phone` 或 `app.users.phone`，表别名会解析回真实表名，未写库名的表按当前库匹配。`COUNT` / `COUNT(DISTINCT ...)` 统计值可以正常读取，同一查询中匹配规则的原始值和拼接值仍会隐藏；`WHERE`、`HAVING`、`EXISTS`、`IN` 子查询里的值不会出现在结果中，不会让其他列被遮蔽。

脱敏按 fail-closed 设计：某个结果列的来源无法确定时（例如无法解析的列名、子查询中可能来自外层查询的列，或 MySQL 自动生成、与表达式文本对不上的列名），只要它可能来自匹配规则的字段，就按敏感处理；SQL 分析失败时同样按保守规则处理。因此少数复杂查询可能多遮几列。

脱敏是结果出口的辅助控制，不是访问授权边界：它基于 SQL 静态分析，无法看到视图定义、存储函数内部或数据库中实际的表结构，也不能排除尚未覆盖的 SQL 写法。绝不能暴露的字段，应在数据库层用只包含安全字段的视图或列级权限隔离，再让本服务使用该账号。

脱敏只替换返回值，不改变 MySQL 的 `WHERE` / `JOIN` 条件或删除结果行。`[REDACTED]` 表示值被隐藏，JSON/compact 的 `masked_columns` 可定位受影响列。已有的非空配置仍会生效；需要全部放开时，把它改成空列表（单连接模式清空环境变量），或删除该配置项。

### 推荐：命名连接

将 [`mysql-connections.example.toml`](../mysql-connections.example.toml) 复制为已被 Git 忽略的 `mysql-connections.toml`，然后集中声明各环境：

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
# 默认不脱敏；需要时在列表中填写字段名或模式。
mask_columns = []
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

每次请求最多读取一次凭据（含连接重试）。`keyring` 或 `command` 较慢时，可设置 `credential_cache_seconds = 30`（0–300，默认 0 即不缓存）在进程内存中短期复用密码：轮换后的新密码最晚在该时长后生效；服务端返回认证失败（1045/1698）、调用 `validate_connections` 或进程退出时立即丢弃缓存。该项只对 `keyring` 和 `command` 有效。

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

`MYSQL_SQL_MODE` 与命名配置的 `sql_mode` 会在每次查询的会话中显式设置（空值即设为空模式）。会改变引号或反斜杠语义的模式会被拒绝：`ANSI_QUOTES`、`NO_BACKSLASH_ESCAPES`，以及包含它们的组合模式 `ANSI`、`DB2`、`MAXDB`、`MSSQL`、`ORACLE`、`POSTGRESQL`，因为只读 SQL 闸门按 MySQL 默认规则识别字符串边界。

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

`MCP_TRANSPORT` 支持 `stdio`、`sse`、`streamable-http`（及别名 `http`）；
未知值会在启动时直接报错，避免配置拼写错误被静默降级为 STDIO。
HTTP/SSE 端口必须在 `1-65535` 范围内；会话空闲超时必须为正有限数。

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
[`ENTERPRISE_DEPLOYMENT.md`](../ENTERPRISE_DEPLOYMENT.md)。

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

## MCP 客户端配置

### Claude Desktop

在 `claude_desktop_config.json` 中加入：

```json
{
  "mcpServers": {
    "mysql-readonly": {
      "command": "npx",
      "args": ["-y", "@yanzhang123/readonly-db-mcp"],
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
      "args": ["-y", "@yanzhang123/readonly-db-mcp"],
      "env": {
        "MYSQL_PROFILES_FILE": "C:/absolute/path/mysql-connections.toml",
        "MYSQL_DEV_PASSWORD": "your_dev_password"
      }
    }
  }
}
```

Windows 宿主如无法解析 `npx`，改用 `npx.cmd`。更多调用场景见 [`MCP_USECASES.md`](../MCP_USECASES.md)。

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
        args: ['-y', '@yanzhang123/readonly-db-mcp']
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
