# MCP 使用反馈优化 开发方案

> 状态：S-1 至 S-3 已实施，未提交（实施记录见 CHANGELOG Unreleased）
> 日期：2026-10-27 ｜ 基线：main@db2e017（工作区另有一处与本任务无关的看板静态资源删除，未提交） ｜ 来源：一次 WMS/PLM/UBP 数据对账任务后的 7 条使用反馈
> 实施范围：`src/mysql_mcp_server/sql_guard.py`、`src/mysql_mcp_server/server.py`、`src/mysql_mcp_server/tool_specs.py`、`tests/`、`tests/snapshots/list_tools.json`、`CHANGELOG.md`、`README.md`；不改用户的连接配置文件，不发版

## 执行卡片

- 改变：① 常见的纯内置函数（`CRC32`、`CONV`、`ORD`、`FIND_IN_SET` 等）不再被当成存储函数/UDF 拦截，做校验和对账不必再配置 `allowed_functions`；② 函数被拦时的报错直接告诉调用方改哪个配置项；③ `timeout_ms` 超过连接上限时，报错和工具描述说明上限来自连接配置、在哪里看、怎么调
- 保持：只读闸门；`BLOCKED_FUNCTIONS`、系统库拦截、`information_schema` 直查拦截；未知函数、UDF、带 schema 前缀的函数调用仍需 `allowed_functions` 放行；各连接的 `query_timeout_ms` 上限和默认值；工具名称、顺序、参数类型
- 先核验：在 HEAD 上跑 `validate_function_safety`，确认 `CRC32`、`CONV`、`ORD`、`FIND_IN_SET` 当前被拦、`REPLACE(...)` 当前放行（本方案的探测结果见 E-2、E-3）；若 `CRC32` 已放行，先停下报告
- 首个切片：S-1
- 完成判据：A-1 至 A-9 通过；完整 pytest 和 mypy 通过

## 背景与目标

- 背景：7 条反馈里，真正落在本仓库代码上的只有函数白名单（反馈 2）和超时上限提示（反馈 4）；其余属于设计如此、连接配置、客户端或其他服务（E-1）。
- 目标：消掉“明明是内置纯函数却被当成 UDF 拦截”的误伤，让被拒的调用方一次就知道怎么改。
- 非目标：
  - 不放开 `information_schema` 直查（反馈 3，设计如此）
  - 不提高任何连接的 `query_timeout_ms`，不改生产连接的库范围（见待裁决）
  - 不处理 Claude Code 的权限分类器（反馈 1）和 `ssh-mcp-server`（反馈 7）
  - 不做数据库适配层，不做 MariaDB 实库验证（用户 2026-10-19 裁决）

## 证据与决策

| ID | 结论 | 证据 | 类型 |
|---|---|---|---|
| E-1 | 7 条反馈的归属：1 分类器无结果、5 中“我拒绝了你的查询”、7 ssh 连接失败——都发生在客户端或另一个服务，本仓库没有分类器逻辑，也不启动 ssh-mcp-server；3 设计如此；4 是连接配置上限；5 的库范围是连接配置；6 见 E-9；2 是代码问题 | `grep -rn classifier` 在 `src/` 无命中；`list_connections` 返回 | 已证实 |
| E-2 | `REPLACE(...)` 作为函数调用已在 0.9.11 放行，`REPLACE INTO` 仍拦。反馈 2 的“REPLACE 被拦”应来自旧版本构建 | `src/mysql_mcp_server/sql_guard.py:74`、`src/mysql_mcp_server/sql_guard.py:160`、`CHANGELOG.md:21`；HEAD 上探测 `SELECT REPLACE(a,'x','y') FROM t` 放行 | 已证实（HEAD）；假设：用户当时运行的是 0.9.10 或更早（推翻条件：升级到 0.9.11 后仍复现，则按新缺陷处理） |
| E-3 | `CRC32`、`CONV`、`ORD`、`FIND_IN_SET`、`UNCOMPRESS`、`STD`、`INET_ATON` 等内置函数在 sqlglot 里解析为未识别函数（Anonymous），被当成可能的存储函数/UDF 拦截；`allowed_functions` 在全部 9 个连接中为空 | `src/mysql_mcp_server/sql_guard.py:122`（安全名单只有时间、用户、UUID 类 20 个）、`src/mysql_mcp_server/sql_guard.py:482`；HEAD 探测 `SELECT BIT_XOR(CRC32(CONCAT_WS('|',a,b)))` 被拒；sqlglot 30.14.0 | 已证实 |
| E-4 | 决策：在 `SAFE_ANONYMOUS_FUNCTIONS` 里补一批“纯内置、无副作用”的函数，归类为哈希/校验与进制转换、字符串与比较、IP 转换、只读 JSON、时间运算。初始候选以 E-3 探测为准，实施时逐个核对：sqlglot 下确为 Anonymous、MySQL 8.4 手册里无副作用、不在 `BLOCKED_FUNCTIONS`；任一不满足就剔除。明确不纳入：`LOAD_FILE`、`BENCHMARK`、锁与等待类（`IS_FREE_LOCK`、`IS_USED_LOCK`、`*_POS_WAIT`）、`NAME_CONST`、`UUID_SHORT`、`RANDOM_BYTES`、`FOUND_ROWS`、`ROW_COUNT`、`AES_*`，以及 `CHECKSUM`（不是 MySQL 函数） | 本方案；该名单与现有时间/UUID 类安全名单同一理由 | Agent 代决（错了的代价：名单漏项只是继续要求配置 `allowed_functions`；误放入有副作用的函数则破坏只读边界，所以每个名字都要有单测，见 A-3） |
| E-5 | 不能放开的前提：不带 schema 前缀的内置函数名总是解析到内置函数；带前缀的调用（`app.crc32()`）解析为存储函数，0.9.11 已要求显式配置，本方案不动 | `src/mysql_mcp_server/sql_guard.py:507`、`CHANGELOG.md:14` | 假设（依据 MySQL 手册对内置函数与存储函数同名的说明；推翻条件：在 MySQL 8.4 上创建同名存储函数后，不带前缀调用执行的是存储函数。此时 E-4 取消，改为只在文档里指导配置 `allowed_functions`） |
| E-6 | 被否决的替代 A：关闭函数检查或默认放行所有未知函数。理由：失去对 UDF 和存储函数的拦截，写权限账号会越过只读边界。会推翻它的前提：部署方确认所有账号都无 EXECUTE 以外的写能力且无 UDF | `src/mysql_mcp_server/sql_guard.py:488` | 已否决替代 |
| E-7 | 被否决的替代 B：只让用户在每个连接里手工填 `allowed_functions`。理由：9 个连接都要改，且把内置函数当成“需审核的 UDF”，配置语义被稀释。保留为兜底，仍有效 | `mysql-connections.example.toml`（`allowed_functions` 注释） | 已否决替代 |
| E-8 | 超时上限：`timeout_ms` 超过连接的 `query_timeout_ms` 时直接抛 `ValueError`，错误信息只写数值，没写配置项名；工具描述只说“不能超过连接上限”，没说上限在 `list_connections` 的 `query_timeout_ms`；JSON schema 的 `maximum` 是 300000，所以 60000 能通过 schema 在运行时才被拒。决策：保持“超限即拒”，不静默截断（截断会让调用方以为用了 60 秒） | `src/mysql_mcp_server/server.py:1588`、`src/mysql_mcp_server/tool_specs.py:93`、`src/mysql_mcp_server/config.py:93`；各连接 `query_timeout_ms` 均为 30000 | 已证实；截断替代被否决（推翻前提：调用方明确希望“不超过上限就尽量大”） |
| E-9 | 乱码只出现在 UBP `dictionarydata` 的部分列注释，且“不影响取值”。服务端连接字符集默认 `utf8mb4`；若是连接字符集问题，会整体乱码而不是部分列。更像注释在库里就是错编码写入 | `src/mysql_mcp_server/config.py:188`、`src/mysql_mcp_server/metadata.py:129` | 假设（推翻条件：在 MySQL 客户端直接 `SHOW FULL COLUMNS` 看同一列显示正常，则是 MCP 读取链路问题，另立任务） |
| E-10 | 生产连接 `honglian-prd` 的 `allowed_databases` 只有 `hl_plm`，且启用了审计；这是配置而不是缺陷 | `list_connections` 返回 `honglian-prd` | 已证实 |

## 方案

### S-1 函数名单与报错（对应反馈 2）

- `SAFE_ANONYMOUS_FUNCTIONS` 按 E-4 补充，并按用途分组写注释；`validate_function_safety` 的判定逻辑不改。
- 被拦时的报错保留函数名列表，把“Add only reviewed deterministic functions to allowed_functions.”改成指明两种配置入口：连接配置的 `allowed_functions`，或旧版单连接环境变量 `MYSQL_ALLOWED_FUNCTIONS`（`src/mysql_mcp_server/config.py:704`），并说明内置函数以外的名字才会走到这里。
- `CHANGELOG.md` 的 Unreleased 下记 Fixed；`README.md:374` 附近的函数策略一句话同步。

### S-2 超时上限提示（对应反馈 4）

- `server.py` 超限错误补上配置项名 `query_timeout_ms`、当前上限，以及“连接配置可调至 300000”。
- `tool_specs.py` 的 `timeout_ms` 描述改为说明上限来自该连接的 `query_timeout_ms`（`list_connections` 可查）。这是对外工具描述的有意变更，仅此一处；刷新 `tests/snapshots/list_tools.json`，其余字段逐字节不变。

### S-3 排障说明（对应反馈 1、3、5、6、7）

- `README.md` 增加一小节“常见问题”，每条一两句：
  - 提示 “auto mode classifier gave no verdict”：来自 Claude Code 的权限分类器，不是 SQL 错误，可原样重试；常用只读工具可在客户端权限里放行；
  - `information_schema` 直查被拒：设计如此，用 `get_schema_info` / `inspect_catalog`；
  - 超时上限：见 S-2；
  - 某库查不到：先看 `list_connections` 的 `allowed_databases` 和 `database_routes`；
  - `ssh-mcp-server` 连接失败属于该服务自身配置；
  - 注释乱码：先在 MySQL 客户端对照同一列（E-9）。
- 不写用户的具体库名、主机或凭据。

## 工作切片

| 切片 | 可观察结果 | 改动范围 | 硬边界 | 依赖 | 验收 |
|---|---|---|---|---|---|
| S-1 | `CRC32` 等内置函数在空 `allowed_functions` 下放行；被拦时报错指向配置项 | `src/mysql_mcp_server/sql_guard.py`、`tests/test_sql_guard.py`、`CHANGELOG.md`、`README.md` | 不改 `BLOCKED_FUNCTIONS`、带前缀调用规则、系统库拦截；不放入 E-4 排除项 | 无 | A-1, A-2, A-3, A-4, A-5, A-9 |
| S-2 | 超限报错和工具描述说明上限来源 | `src/mysql_mcp_server/server.py`、`src/mysql_mcp_server/tool_specs.py`、`tests/snapshots/list_tools.json`、相关测试 | 不改上限值和默认值；不改参数类型、名称、顺序 | 无 | A-6, A-7 |
| S-3 | README 有常见问题小节 | `README.md` | 只写说明，不改行为 | S-1、S-2 的最终措辞 | A-8 |

切片顺序与粒度是建议；执行 Agent 可合并或重排，但不得越过硬边界。

## 验收

| ID | 场景与输入 | 期望 | 必须保持的拒绝 / 隔离 / 不执行 | 验证方式（计划，未运行） |
|---|---|---|---|---|
| A-1 | 空 `allowed_functions`，`SELECT BIT_XOR(CRC32(CONCAT_WS('|',a,b))) FROM t` | 通过读写校验和函数校验 | — | `tests/test_sql_guard.py` 新增用例；预期通过 |
| A-2 | `SELECT my_udf(a)`、`SELECT app.crc32(a)`、`SELECT app.now()` | 仍被拦并提示配置 `allowed_functions`；配置对应名字后放行 | 带前缀调用不因内置名单而放行 | 同上，沿用 `tests/test_sql_guard.py:339` 的写法 |
| A-3 | 对新增名单逐个断言：在 sqlglot 下为 Anonymous、不在 `BLOCKED_FUNCTIONS`、不在 E-4 排除项 | 名单漂移（如升级 sqlglot 后某函数被建模）或误加排除项时测试失败 | `SLEEP`、`GET_LOCK`、`LOAD_FILE`、`BENCHMARK` 等仍被 `validate_read_only_query` 拒绝 | 参数化单测 |
| A-4 | `SELECT REPLACE(a,'x','y') FROM t` | 放行 | `REPLACE INTO t ...`、`WITH x AS (...) REPLACE ...` 仍被拒 | 沿用 0.9.11 用例，确认未回退 |
| A-5 | 未知函数被拦的报错文本 | 含函数名、`allowed_functions`、`MYSQL_ALLOWED_FUNCTIONS` | 报错不回显 SQL 字面量 | 单测断言消息 |
| A-6 | 连接上限 30000，`timeout_ms=60000` | 抛 `ValueError`，消息含 `query_timeout_ms`、30000、300000；`timeout_ms=30000` 和 `100` 行为不变 | 不静默截断，不连接数据库 | `tests/test_execution.py` 或同类，新增用例 |
| A-7 | `list_tools()` 输出对比快照 | 仅 `timeout_ms` 的 description 变化 | 工具名、顺序、inputSchema 类型和 annotations 不变 | 刷新快照后对比 git diff，只应有 description 行 |
| A-8 | README 常见问题 | 不含库名、主机、凭据、用户标识 | — | 人工通读 |
| A-9 | `SELECT * FROM information_schema.TABLES` | 仍被拒，提示改用 `get_schema_info` 等 | 系统库拦截不变 | 沿用现有用例 |

另需：完整 `pytest` 和 `mypy` 通过。可选的真实库核对（`MYSQL_MCP_LOCAL_TESTS=1`，本地 MySQL 8.4，只做 `SELECT CRC32('a')` 这类只读语句）用来确认 E-5；不做写入。

## 兼容、风险与回滚

- 兼容：只会让原来被拒的内置函数调用变为放行，已放行的调用不受影响；报错文案和工具描述变化可能影响依赖精确文本的客户端，快照测试覆盖前者，后者见 A-7。
- 风险：名单若误含有副作用的函数会削弱只读边界，靠 E-4 的逐项核对和 A-3 兜底；E-5 若被推翻，需撤回 S-1 的名单部分，只保留报错文案。
- 回滚：S-1、S-2 互相独立，可分别回退；不涉及数据、配置格式或发布流程。
- 未覆盖：E-2、E-9 未在用户环境复现。

## 待裁决

以下为配置层面的建议，不属于代码切片，不阻塞 S-1 至 S-3。

| ID | 问题 | 证据冲突或缺口 | 影响 | 裁决前可继续的部分 |
|---|---|---|---|---|
| Q-1 | 是否给 `honglian-prd` 增加 WMS、UBP 库的可见范围 | 当前仅 `hl_plm`（E-10）；生产库暴露范围由你和 DBA 决定，且账号权限不由本仓库控制 | 配置文件，无代码改动 | 全部切片 |
| Q-2 | 是否把测试类连接的 `query_timeout_ms` 调到 60000 以上 | 当前均为 30000（E-8）；生产是否保持 30000 也由你决定 | 配置文件，无代码改动 | 全部切片 |
