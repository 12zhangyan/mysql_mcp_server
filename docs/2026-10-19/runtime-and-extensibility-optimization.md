# 查询热路径与可扩展性优化 开发方案

> 状态：已实施·未提交（S-1 至 S-6）｜ Q-1 已裁决：暂不扩展，S-7 取消
> 修订：2026-10-19 用户裁决 Q-1 为“暂不扩展”，取消 S-7 及其验收 A-14；A-13 不再依赖 S-7。其余约束继续有效
> 日期：2026-10-19 ｜ 基线：main@575e17b + 未提交的"单次解析 SQL"改动（server.py / sql_guard.py / 2 个测试文件 / CHANGELOG） ｜ 来源：本次对话中的 7 项优化清单，用户要求"全部都做，先出方案"
> 实施范围：`src/mysql_mcp_server/`、`tests/`、`CHANGELOG.md`、`README.md`（仅新增配置项说明）；不改 npm 启动器、发布脚本和 CI

## 执行卡片

- 改变：减少每次查询的凭据查找、数据库往返、排队唤醒延迟和审计文件系统调用；把工具定义和分发改为注册表，把审计和错误上下文收拢成一个对象，为数据库适配层留出接口
- 保持：只读闸门、只读会话和无条件回滚；对外工具列表（名称、描述、inputSchema、annotations、顺序）逐字节不变；错误码、错误消息、审计字段不变；`mysql_mcp_server.server` 中被测试 patch 的符号（`_open_connection`、`connect`、`run_query`、`get_db_config`、`close_runtime_resources`、`connection_pool_manager`）保留在原模块，并在调用时按模块全局名查找
- 先核验：① 连接池借出的 `PooledMySQLConnection` 能透传 `server_info`（S-2 依赖它）；② 在 HEAD 上生成 `list_tools()` 的 JSON 快照（S-5 的回归基准）。任一项不符先停下报告
- 首个切片：S-6（上下文收拢，纯重构，后续切片都会碰到这段代码）
- 完成判据：A-1 至 A-13 通过；完整 pytest 和 mypy 通过

## 背景与目标

- 背景：上一轮已把 SQL 解析合并为一次。这一轮的 7 项是从代码中确认的剩余冗余（E-1 至 E-6）和扩展障碍（E-7 至 E-9）
- 非目标：
  - 不新增任何数据库实现，也不抽数据库适配层（Q-1 裁决，S-7 已取消）
  - 不跨请求缓存 SQL 或结果
  - 不改变连接池的 `pool_reset_session` 策略
  - 不实现排队公平性（FIFO）
  - 不修复 HEAD 上已存在的 black 导入格式问题

## 证据与决策

| ID | 结论 | 证据 | 类型 |
|---|---|---|---|
| E-1 | 使用 keyring 时，每次查询查两次凭据库：一次在 `runtime_status()`，一次在建连时组装配置 | `src/mysql_mcp_server/config.py:162`、`src/mysql_mcp_server/config.py:1027`、`src/mysql_mcp_server/server.py:1800` | 已证实 |
| E-2 | 使用 command 时，每次查询（含连接重试的每次尝试）都启动一次子进程；连接池不能避免，因为池缓存键要用密码哈希 | `src/mysql_mcp_server/config.py:150`、`src/mysql_mcp_server/runtime.py:246` | 已证实 |
| E-3 | 决策：凭据 TTL 缓存做成按连接配置的可选项 `credential_cache_seconds`，默认 0（关闭），上限 300；只对 keyring 和 command 生效。默认值保持现有行为，所以"新密码最晚多久生效"这个安全取舍交给部署方配置，不阻塞实施 | 本方案 | 决策 |
| E-4 | 会话设置每次查询往返 3 次，结束后再回滚 1 次 | `src/mysql_mcp_server/server.py:246`、`src/mysql_mcp_server/server.py:252`；测试固定了这个顺序：`tests/test_execution.py:90` | 已证实 |
| E-5 | 握手阶段已拿到服务端版本串，读取 `server_info` 不产生网络往返；纯 Python 和 C 扩展两种驱动都支持 | Connector/Python 9.7.0 源码：`MySQLConnectionAbstract.server_info` 读取 `_handshake["server_version_original"]`，`CMySQLConnection` 在握手时填入 | 已证实 |
| E-6 | 决策：只有当 `server_info` 能解析为 MySQL ≥ 5.7.20 且不含 `MariaDB` 时，才把两条 SET 合成 `SET SESSION MAX_EXECUTION_TIME = n, SESSION transaction_read_only = ON`；其余情况（MariaDB、旧版本、无法解析、mock 对象）走现有三条语句的路径。MySQL 的多变量 SET 要么全部生效、要么全部不生效，所以合并后语义不变 | MySQL 8.4 手册对 SET 语句和 `transaction_read_only`（5.7.20 引入）的说明 | 决策；假设（推翻条件：S-2 实库验收中合并语句失败，或读回的 `@@transaction_read_only` 不为 1） |
| E-7 | 否决的替代：先试合并语句，遇到 1193/1064 错误再降级。否决理由：MariaDB 上每次查询会多一次失败的往返，性能反而变差 | 本方案 | 已否决替代 |
| E-8 | 排队靠每 10ms 轮询一次；释放名额的代码运行在工作线程里（`lease.run` 的 finally） | `src/mysql_mcp_server/runtime.py:418`、`src/mysql_mcp_server/runtime.py:374` | 已证实 |
| E-9 | 进程运行在 asyncio 事件循环上（STDIO 通过 `asyncio.run`，HTTP/SSE 通过 uvicorn） | `src/mysql_mcp_server/__init__.py:20`、`src/mysql_mcp_server/__main__.py:10` | 已证实 |
| E-10 | 决策：每个排队者登记一个 `asyncio.Event`；释放名额时用 `loop.call_soon_threadsafe(event.set)` 唤醒同一连接配置下的排队者，工作线程不会被阻塞；唤醒后仍在锁内重新检查名额。保留最长 50ms 的兜底超时，防止漏唤醒导致一直等待 | 本方案 | 决策 |
| E-11 | 否决的替代：`anyio.from_thread.run_sync` 会阻塞工作线程，等事件循环处理完才返回；配合 `abandon_on_cancel=True`，事件循环关闭时可能出错 | anyio 4.13.0 | 已否决替代 |
| E-12 | 每写一条审计都要建目录、检查文件是否存在、读取大小、打开、关闭；每次查询至少写两条 | `src/mysql_mcp_server/audit.py:226`、`src/mysql_mcp_server/audit.py:228` | 已证实 |
| E-13 | 决策：只做系统调用精简——每个路径成功建过一次目录后不再建；把"检查存在 + 读取大小"合并为一次读取大小，并捕获文件不存在的情况；每次写入仍然打开再关闭文件 | 本方案 | 决策 |
| E-14 | 否决的替代：长期持有审计文件句柄。否决理由：Windows 上打开的文件不能被外部日志工具重命名或删除，会改变现有的运维行为；内部轮转也必须先关闭句柄；收益只有每条约一次打开和关闭。会推翻这个选择的前提：部署方确认只在 POSIX 上运行，且审计写入成为可测的瓶颈 | `src/mysql_mcp_server/audit.py:181`（`_rotate` 直接重命名路径） | 已否决替代 |
| E-15 | `list_tools` 用约 480 行手写 12 个工具的参数定义，`call_tool` 用 `if name == ...` 逐个分发 | `src/mysql_mcp_server/server.py:686`、`src/mysql_mcp_server/server.py:1183`、`src/mysql_mcp_server/server.py:1533` | 已证实 |
| E-16 | 测试 patch 了 `mysql_mcp_server.server` 上的 6 个符号（`_open_connection` 20 处、`connect` 8 处、`run_query` 7 处……） | `grep -rn 'patch("mysql_mcp_server.server.' tests` | 已证实 |
| E-17 | 决策：注册表只承载"定义 + 分发"。参数定义移到新模块 `src/mysql_mcp_server/tool_specs.py`（拟新增，纯数据）；处理函数仍放在 `server.py`，通过装饰器注册；执行核心不搬家，以保证 E-16 的 patch 继续有效 | 本方案 | 决策 |
| E-18 | `execute_query` 有 15 个参数；目标字段（`requested_connection/requested_database/route_applied`）在审计调用和错误补全中手工重复传入，两段 `payload.setdefault` 补全代码完全相同 | `src/mysql_mcp_server/server.py:1906`、`src/mysql_mcp_server/server.py:2157`；审计调用见 `src/mysql_mcp_server/server.py:1885`、`src/mysql_mcp_server/server.py:2102`、`src/mysql_mcp_server/server.py:2133`、`src/mysql_mcp_server/server.py:2181` | 已证实 |
| E-19 | MySQL 专用逻辑分散在 sqlglot 方言参数（`read="mysql"`）、`information_schema` 元数据 SQL、会话设置和错误分类等多处 | `src/mysql_mcp_server/sql_guard.py:296`、`src/mysql_mcp_server/metadata.py:185`、`src/mysql_mcp_server/server.py:252`、`src/mysql_mcp_server/errors.py:43` | 已证实 |

## 方案

### 1. 凭据（S-1）

- 新增一个解析入口：返回"是否就绪 + 密码"。`execute_query` 和 `_sync_resource_query` 在每次请求里只调用它一次。`build_connector_config` 新增可选参数 `password`，由调用方传入；不传时行为与现在相同，供 `get_db_config` 等旧入口使用。
- 连接重试复用同一个已解析的密码。
- 设置 `credential_cache_seconds > 0` 时，进程内缓存 `(provider, service, ref, command)` → 密码 + 过期时间。以下情况清空缓存：
  - 调用 `validate_connections`（强制重新加载配置）；
  - 执行 `close_runtime_resources`；
  - 建连时返回认证失败（errno 1045、1698）。只清该条缓存，不自动重试。
- 不在任何日志、审计或错误信息中输出密码或缓存键原文。

### 2. 会话设置（S-2）

- `_open_connection` 之后读取 `connection.server_info`，按 E-6 判断能否合并：
  - 能合并：执行一条合并的 SET，再执行 `START TRANSACTION READ ONLY`，共 2 次往返。
  - 不能合并：执行现有的 `_apply_server_query_timeout` + `_start_read_only_transaction`，次序和语句不变，包括 MariaDB 和"服务端不支持超时"的降级。
- 判断只依赖握手信息，不缓存跨连接状态。

### 3. 排队（S-3）

- `QueryAdmission` 为每个连接配置维护一组排队者的事件和对应的事件循环。
- `release` 在锁内把名额减 1，再对该组排队者调用 `call_soon_threadsafe(event.set)`；如果事件循环已关闭，忽略该排队者。
- `acquire` 的循环改为：在超时时间、剩余排队时间和 50ms 三者中取最短，等待事件，醒来后在锁内重新检查名额。
- "排队已满"和"排队超时"的错误、`queue_wait_ms` 统计、`QueryLease` 的状态机都保持不变。

### 4. 审计（S-4）

按 E-13 精简；轮转逻辑和 fsync 行为不变。

### 5. 工具注册表（S-5）

- `tool_specs.py` 用有序列表定义 `ToolSpec(name, description, input_schema, annotations)`，复用的参数片段（connection、database、audit_context、结果分页参数等）用函数构造。
- `server.py` 用 `@tool("name")` 把处理函数绑定到对应的 spec。
- `call_tool` 保留公共前置和后置处理：审计上下文、日志、database 标识符校验、`QueryFailure` 转错误结果、重置审计上下文。中间改为查表分发；工具名未注册时仍抛出 `Unknown tool: {name}`。
- 启动时自检：每个 spec 都恰好有一个处理函数，没有缺少的，也没有多余的。
- `query` 别名保持现有行为。

### 6. 请求上下文（S-6）

- 新增一个不可变的请求上下文，内容为 `target` 加上惰性计算的 `query_id`。提供两个方法：
  - `audit(status, **字段)`：自动带上目标字段后调用 `_write_audit_event`；
  - `enrich(exc)`：替代两段相同的 `payload.setdefault` 补全代码。
- `_write_audit_event` 的签名和输出字段不变，供其他调用方使用。
- `execute_query` 的对外参数不变，这一项只收拢内部重复代码。

### 7. 数据库适配层（S-7，已取消）

- 定义 `DatabaseDialect` 协议，包含：sqlglot 方言名、系统库集合、会话设置（吸收 S-2 的逻辑）、元数据 SQL 构造（吸收 `metadata.py` 和 `_catalog_query`）、驱动错误分类（吸收 `errors.database_failure`）。
- `MySQLDialect` 是唯一实现，现有代码平移进去，行为不变。
- 连接配置暂不暴露方言选项。接口的形状取决于第二个目标数据库，见 Q-1。

## 工作切片

| 切片 | 可观察结果 | 改动范围 | 硬边界 | 依赖 | 验收 |
|---|---|---|---|---|---|
| S-6 | 审计和错误补全通过请求上下文完成，重复代码删除 | `server.py` | 审计 JSON 字段和取值、`QueryFailure` 的 payload 不变 | — | A-12 |
| S-1 | 每次请求最多解析一次凭据；可选 TTL 缓存 | `config.py`、`credential_store.py`、`server.py`、`runtime.py`（`close_runtime_resources`）、README | 默认值 0 时与现在的行为等价；凭据不出现在任何输出中 | S-6 | A-1、A-2、A-3 |
| S-2 | MySQL ≥ 5.7.20 时会话设置减为 2 次往返 | `server.py` | MariaDB 和无法识别版本的语句序列不变；只读事务和回滚不变 | S-6 | A-4、A-5、A-6 |
| S-3 | 排队者在名额释放时立即被唤醒 | `runtime.py` | 错误码、上限、`QueryLease` 状态机不变 | — | A-7、A-8 |
| S-4 | 审计每条写入的系统调用减少 | `audit.py` | 轮转、fsync、"写入失败即拒绝"语义不变 | — | A-9 |
| S-5 | 工具由注册表定义和分发 | `server.py`、`tool_specs.py`（拟新增） | `list_tools` 输出逐字节不变；E-16 的 patch 目标保留 | S-6 | A-10、A-11 |
| ~~S-7~~ | 已取消（Q-1）：MySQL 逻辑集中到适配层 | 新增 `dialects/`（拟新增）、`sql_guard.py`、`metadata.py`、`errors.py`、`server.py` | 零行为变化；不新增数据库实现 | S-2、S-5、Q-1 | A-13、A-14 |

切片顺序和粒度只是建议，执行 Agent 可以合并、细分、重排或并行委派，但不得越过硬边界。S-3、S-4 与其他切片独立，可以并行。

## 验收

| ID | 场景与输入 | 期望 | 必须保持的拒绝 / 隔离 / 不执行 | 验证方式（计划，未运行） |
|---|---|---|---|---|
| A-1 | keyring 方式的配置，执行一次 `execute_sql`，其中包含一次 errno=-1 重试 | 读取凭据库恰好 1 次 | 未就绪的配置仍在连接数据库之前被拒绝 | 新增单测，mock `get_keyring_password` 并统计调用次数 |
| A-2 | command 方式，`credential_cache_seconds=30`，在 TTL 内连续查询 3 次，然后推进时间超过 TTL 再查 1 次 | 子进程共启动 2 次 | 默认值 0 时每次请求启动 1 次；密码不出现在日志、审计和错误信息中 | 新增单测，mock `subprocess.run` 和时钟 |
| A-3 | 开启缓存后建连返回 errno 1045 | 本次返回原来的认证错误，下次请求重新取凭据 | 不自动重试 | 新增单测 |
| A-4 | mock 连接的 `server_info="8.4.6"` | 执行的语句依次为合并的 SET、`START TRANSACTION READ ONLY`、查询本身 | 结束时仍然回滚 | 新增单测，断言完整的语句序列 |
| A-5 | `server_info` 分别为 `"10.11.6-MariaDB"`、`"5.5.5-10.6.12-MariaDB"`、`"5.7.19"`、`None`、MagicMock | 语句序列与现在完全相同 | MariaDB 超时降级路径不变 | 参数化单测；现有 `tests/test_execution.py:90` 的断言不改也能通过 |
| A-6 | 真实 MySQL 8.x（`MYSQL_MCP_LOCAL_TESTS=1`） | 查询中读回 `@@transaction_read_only=1` 和 `@@max_execution_time=n`；尝试写入被服务端拒绝 | 不执行任何 DML 或 DDL | 本地实库用例；没有环境时标为未验证 |
| A-7 | `max_concurrent_queries=1`，占用名额后另一协程排队，再从工作线程释放名额 | 排队者在 5ms 内拿到名额（p95，测 50 次） | 排队已满、排队超时仍返回 `CONNECTION_BUSY` | 新增单测 + 微基准，记录改动前后的数据 |
| A-8 | 排队期间取消请求或超时 | 排队计数恢复为 0，没有遗留的事件 | 不泄漏名额 | 新增单测 |
| A-9 | 开启审计文件，连续写入 1000 条 | 内容和轮转结果与现在相同；耗时下降（记录前后数据，不设硬阈值） | 写入失败时"拒绝查询"的语义不变 | 现有 `tests/test_audit.py` + 新增"文件被外部删除后自动重建"用例 |
| A-10 | 调用 `list_tools()` | 输出 JSON 与 HEAD 快照逐字节相同 | — | 新增快照测试（快照在改动前于 HEAD 上生成） |
| A-11 | 调用每个工具，外加一个未注册的工具名 | 结果与现在相同；未注册名返回 `Unknown tool: x` | 注册表自检失败时进程拒绝启动 | 完整 pytest + 新增自检单测 |
| A-12 | 在各个失败点（配置解析、校验、排队、执行、超时、取消）触发失败 | 审计 JSON 和错误 payload 与改动前逐字段相同 | — | 改动前在 HEAD 上录制审计和错误样本，改动后对比 |
| A-13 | 完整 pytest，包括本地实库用例 | 全部通过，跳过的用例数量不变 | — | `pytest -q` |
| ~~A-14~~ | 已取消（Q-1）。检查 `src` 中 `read="mysql"` 和 `information_schema` 字面量的分布 | 只出现在 `dialects/mysql` 中 | — | grep 断言 |

## 兼容、风险与回滚

- 每个切片独立提交，回滚时按切片 revert。S-1 的新配置项默认关闭，回滚无需迁移数据。
- S-2 的风险：MySQL 分支、代理或 Aurora 等返回的版本串与真实能力不一致。合并语句失败时按错误原样报告，不在执行中降级。缓解办法：A-6 实库验收；如有必要，再加一个连接配置开关强制走旧路径（只有出现真实反例时才加）。
- S-3 的风险：跨线程唤醒的竞态。保留锁内重新检查和 50ms 兜底超时；A-8 覆盖取消场景。
- S-5、S-7 是大范围机械搬移。用 A-10、A-12 的快照对比兜底；如果冲突难以合并，可以让 S-7 单独成为一个 PR。
- 未覆盖：生产负载下的吞吐量。本方案的计时只能证明本机微基准上的改善。
- MariaDB 实库：用户已裁决不需要验证（2026-10-19）。MariaDB 走原语句序列，由 A-5 的模拟用例覆盖。

## 待裁决

| ID | 问题 | 证据冲突或缺口 | 影响 | 裁决前可继续的部分 |
|---|---|---|---|---|
| Q-1 | 【已裁决：暂不扩展，取消 S-7】适配层的第二个目标数据库是哪一个（PostgreSQL / SQL Server / 达梦、人大金仓 / 暂不扩展）？ | 目前没有第二个目标数据库，接口形状只能靠猜。例如 PostgreSQL 有"库 + schema"两层，只读会话和超时的写法也不同。如果暂不扩展，建议取消 S-7，以免引入用不上的抽象 | S-7、A-13、A-14 | S-1 至 S-6 全部可以继续 |

## 实施记录（2026-10-19）

### 相对方案的偏差

- S-2：方案原写"合并语句失败时按原错误报告"。实施时改为：合并的 SET 遇到 1193/1064（语法错误或未知变量）时，回退到原来的三条语句。只有版本串像 MySQL、实际却不支持 `transaction_read_only` 的兼容库才会走这条回退；真正的 MySQL 不会触发，所以不额外增加往返。其他错误码照常抛出。
- S-4：在 E-13 的基础上，额外按配置的路径字符串缓存 `resolve()` 结果。实测这一步单次约 330µs，是开销最大的一项。轮转、fsync、写入失败即拒绝的语义都不变；目录被外部删除后，下次写入时会重建。
- A-6：没有做"尝试写入被服务端拒绝"这一步，因为它需要执行 DDL/DML，与只读约束冲突。改为读回 `@@SESSION.transaction_read_only` 和 `@@SESSION.max_execution_time` 来验证。
- A-10：快照比较改为比较序列化后的文本，从而把键顺序也固定下来。

### 验收结果

| ID | 结果 |
|---|---|
| A-1、A-2、A-3 | 通过：`tests/test_credential_cache.py`（9 项） |
| A-4、A-5 | 通过：`tests/test_execution.py` 新增的会话设置参数化用例，另有回退用例和"非预期错误不被掩盖"用例 |
| A-6 | 通过：本机 MySQL 8.4.0 上，`test_real_combined_session_setup_enforces_read_only_and_timeout` 走了合并路径，读回 `transaction_read_only=1`、`max_execution_time=4321`；本地实库用例共 29 项全部通过 |
| A-7 | 通过：线程释放名额后排队者的唤醒延迟（50 次），p95 从 16.05ms 降为 0.20–0.31ms；另有一个与计时无关的单测（兜底超时拉长到 5s，仍能在 1s 内唤醒） |
| A-8 | 通过：取消和超时后，`_waiters`、`_waiting` 都为空；事件循环已关闭时调用释放不报错 |
| A-9 | 通过：1000 次写入耗时从 1475ms 降为约 700ms；无轮转和有轮转两种场景下，生成的文件名、行数和内容摘要都与改动前一致；新增"目录被删除后重建"用例 |
| A-10、A-11 | 通过：`tests/test_tool_registry.py`；真实 STDIO 握手返回 12 个工具，未注册工具名的错误文本不变 |
| A-12 | 通过：18 个场景的工具响应、执行的 SQL 序列和审计事件，与改动前的记录逐字节一致 |
| A-13 | 通过：默认环境 447 passed、29 skipped；开启本地实库后 476 passed、0 skipped；mypy 检查 13 个文件通过 |

