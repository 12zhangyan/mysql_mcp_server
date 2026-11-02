# 连接池复用与 SSH 隧道隔离 开发方案

> 状态：草稿·实施中（Q-1 至 Q-3 已于 2026-11-01 裁决）
> 日期：2026-11-01 ｜ 基线：main@097a591 ｜ 来源：本会话代码审查 IM-1、MI-1、IM-2 及修正方案讨论
> 实施范围：`src/mysql_mcp_server/runtime.py`、`src/mysql_mcp_server/server.py`（连接释放路径）、`src/mysql_mcp_server/config.py`（连接参数与旧单连接默认值）、`tests/`、`docs/CONFIGURATION.md`、`CHANGELOG.md`；不改工具列表（`tests/snapshots/list_tools.json` 不得变化）

## 执行卡片
- 改变：同一连接在多个库之间切换、或调用传入不同 `timeout_ms` 时复用已有连接池，不再销毁重建；一个 SSH 隧道启动慢不再阻塞其他连接；隧道本地端口被其他程序占用时报错，而不是连到错误的服务器。
- 保持：每次查询仍单独设置 `MAX_EXECUTION_TIME`、`transaction_read_only`、`sql_mode`（`server.py` `_prepare_read_only_session`）；密码轮换、隧道重启、`pool_size` 变化仍会整体换池；`pool_size=0` 仍直连；每个 profile 的并发准入上限不变；TLS 校验失败仍丢弃对应的池。
- 先核验：运行本会话的模拟脚本（假 pool factory、交替库 `a/b` 调用 5 次），确认在基线上创建了 5 个池；如果只创建了 1–2 个，说明 E-1 不成立，先停下报告。
- 首个切片：S-1。
- 实现约束：只为 A-n 与 E-n 写代码；新增的类、层次、校验、兜底或重试须能指到其中一条；不新增依赖。
- 完成判据：A-1…A-9 通过；`pytest`、`mypy src` 通过；CI 的 MySQL 实库测试通过。

## 背景与目标
- 背景：连接池的 key 包含库名和由 `timeout_ms` 算出的 socket 超时（E-1、E-2）。key 一变就销毁旧池，而建新池会一次建满 `pool_size` 条连接（E-3），所以跨库查询每次都要新建多条连接，正在使用的连接还会被丢给已销毁的旧池（E-4）。SSH 隧道管理器用一把全局锁包住隧道启动的等待（E-7）；就绪检查只看端口能不能连上（E-8）。
- 非目标：不自己实现连接池，也不改用其他连接库；不改变每次查询的会话设置；不做 MariaDB 适配；不处理 Agent 并发以外的性能优化。

## 证据与决策
| ID | 结论 | 证据 | 类型 |
|---|---|---|---|
| E-1 | 池 key 是对整个 connector 配置取哈希（含 `database`、`read_timeout`、`write_timeout`）；同一 profile 的 key 一变，旧池就被销毁 | `src/mysql_mcp_server/runtime.py:243`、`src/mysql_mcp_server/runtime.py:287`；本会话模拟：交替库调用 5 次创建 5 个池，交替 `timeout_ms` 调用 3 次创建 3 个池 | 已证实 |
| E-2 | socket 超时由单次调用的 `timeout_ms` 推出，并写进 connector 配置 | `src/mysql_mcp_server/config.py:1009`、`src/mysql_mcp_server/server.py:1652` | 已证实 |
| E-3 | `MySQLConnectionPool` 创建时一次建满 `pool_size` 条连接；命名连接默认 `pool_size=5` | connector 9.7 `pooling.py:489`；`docs/CONFIGURATION.md:93` | 已证实 |
| E-4 | `_dispose_pool` 只清空空闲连接；正在使用的连接 `close()` 时会还回旧池，没有主动关闭，要等垃圾回收才释放 | `src/mysql_mcp_server/runtime.py:217`；connector 9.7 `pooling.py:398` | 已证实 |
| E-5 | C 扩展在连接建立后不允许修改 `read_timeout`/`write_timeout`，会抛 `ProgrammingError`；`use_pure` 默认为 None，装了 C 扩展就会用它 | connector 9.7 `connection_cext.py:228`；`src/mysql_mcp_server/config.py:226` | 已证实 |
| E-6 | 单次调用的截止由 `anyio.fail_after` 控制，超时后取消；C 扩展没有 `shutdown()`，取消时只能退回 `close()`，代码注释也说明 C 扩展无法关闭 socket | `src/mysql_mcp_server/server.py:1796`、`src/mysql_mcp_server/server.py:492`；connector `abstracts.py:1092` | 已证实 |
| E-7 | `SshTunnelManager.endpoint` 对所有 profile（包括直连的）都拿同一把 RLock，持锁时会执行 `_start`，最长等 `startup_timeout`（上限 60 秒） | `src/mysql_mcp_server/runtime.py:104`、`src/mysql_mcp_server/runtime.py:114`、`src/mysql_mcp_server/runtime.py:132` | 已证实 |
| E-8 | 就绪检查只判断 `127.0.0.1:local_port` 能否连接，不确认对端是自己启动的 ssh；`ExitOnForwardFailure` 要在 SSH 认证之后才会因绑定失败退出 | `src/mysql_mcp_server/runtime.py:170` | 已证实（触发路径来自代码阅读，未实测） |
| E-9 | 旧单连接模式的 `MYSQL_LOCAL_PORT` 默认值在代码里是 3330，文档写的是 0（自动选端口） | `src/mysql_mcp_server/config.py:793`、`docs/CONFIGURATION.md:235` | 已证实 |
| E-10 | 每个 profile 的并发准入上限是 `min(max_concurrent_queries, pool_size)`，所以同一 profile 同时借出的连接数不会超过 `pool_size`，和池的个数无关 | `src/mysql_mcp_server/runtime.py:401` | 已证实 |
| E-11 | 池 key 去掉超时，socket 超时统一用 profile 的 `query_timeout_ms`；单次调用更短的 `timeout_ms` 由 `fail_after` 和服务器端 `MAX_EXECUTION_TIME` 控制 | E-2、E-5、E-6；Q-2 | 用户已裁决（接受） |
| E-12 | 同一 profile 按库保留多个池（库名为 None 也单独算一个池），超过上限时淘汰最久未用的池；上限由新增配置项 `max_database_pools` 控制（默认 3，范围 1–16，旧单连接模式对应 `MYSQL_MAX_DATABASE_POOLS`），每个池沿用 `pool_size` | E-1、E-3、E-10；Q-1 | 用户已裁决（新增配置项）；默认值与范围为 Agent 代决（错了的代价：用户需要自行调整配置） |
| E-13 | 被否决的替代：每个 profile 只建一个池，取出连接后用 `cmd_init_db` 切库。否决理由：MySQL 无法把连接恢复成「未选库」，不传 database 的调用会悄悄在上一次的库上执行，审计记录也对不上；每次查询还要多一次往返，而读 `cnx.database` 本身也要执行一次 `SELECT DATABASE()`。如果 Q-1 的多池上限不可接受，再重新评估这个方案 | connector `connection.py:1258`；E-12 | 已否决替代 |
| E-14 | 被否决的替代：按连接设置超时。否决理由：C 扩展不支持（E-5） | E-5 | 已否决替代 |
| E-15 | 被淘汰或被销毁的池标记为「已退役」；借出的连接归还时，如果它的池已退役，就关闭底层连接，不再放回池里 | E-4 | Agent 代决（错了的代价：仍要靠垃圾回收释放连接，服务器端连接数会短暂超出预期） |
| E-16 | 隧道锁改为每个 profile 一把，另用一把短持有的锁保护锁字典和隧道表；`close_all` 要先拿到各 profile 的锁 | E-7 | Agent 代决（错了的代价：并发启动或关闭时可能重复起 ssh 进程） |
| E-18 | 旧单连接模式的 `MYSQL_LOCAL_PORT` 默认值保持 3330，把文档改成 3330 | E-9；Q-3 | 用户已裁决 |
| E-17 | 启动 ssh 前先确认本地端口空闲，被占用就报错；判定就绪时同时确认 ssh 进程还活着。无法用标准库确认监听端口属于哪个进程，所以「预检之后、ssh 绑定之前被别人抢占」这个时间窗仍然存在，作为残余风险写入文档 | E-8 | Agent 代决（错了的代价：极少数情况下仍可能连到错误的监听方） |

## 方案
1. **池 key**：只包含 profile 名、`pool_size`、隧道端点（含 generation）、凭据哈希和 `database`；不包含 `read_timeout`/`write_timeout`。`build_connector_config` 的 socket 超时改为固定按 profile 的 `query_timeout_ms` 计算（E-11）。
2. **多池**：`ConnectionPoolManager` 为每个 profile 维护一个按库区分、按最近使用排序的池集合。命中就复用；新库超出上限时淘汰最久未用的池。除库名以外的 key 变化（密码、端点、`pool_size`）仍然退役该 profile 的全部池（E-12）。
3. **退役池**：淘汰或销毁的池加入退役集合。`server.py` 的 `_release_connection` 改为交给管理器处理：池已退役就关闭底层连接，否则照常归还（E-15）。
4. **SSH 锁**：每个 profile 一把锁；不使用 SSH 的 profile 只在清理旧隧道时短暂持锁（E-16）。
5. **端口归属**：端口预检，加上就绪时确认进程仍存活（E-17）；按 Q-3 的裁决调整旧单连接模式的默认端口。

## 工作切片
| 切片 | 可观察结果 | 改动范围 | 硬边界 | 依赖 | 验收 |
|---|---|---|---|---|---|
| S-1 | 跨库和变超时调用复用池；淘汰的池里正在用的连接，归还时会被关闭 | `runtime.py` `ConnectionPoolManager`；`config.py` `build_connector_config`；`server.py` `_release_connection` 及其调用方；`tests/test_runtime.py`、`tests/test_local_readonly.py` | 不改 `_prepare_read_only_session`；`pool_size=0` 直连路径不变；只新增 `max_database_pools` 一个配置项 | 无 | A-1…A-5 |
| S-2 | 一个 SSH 隧道启动慢，不阻塞其他 profile | `runtime.py` `SshTunnelManager`；`tests/test_runtime.py` | 隧道复用、重启、generation 语义不变 | 无 | A-6 |
| S-3 | 端口被占用时报错；ssh 提前退出时不会误判为就绪 | `runtime.py` `_start`；`docs/CONFIGURATION.md`（`MYSQL_LOCAL_PORT` 默认值）；`tests/test_runtime.py` | 不新增依赖；不改旧单连接默认端口 | S-2（同一文件，建议先做 S-2） | A-7…A-9 |

切片顺序与粒度是建议；执行 Agent 可合并、细分、重排或并行委派，但不得越过硬边界。CHANGELOG 的 `[Unreleased]` 要记录新配置项 `max_database_pools`、Q-2 的超时语义变化和端口预检。

## 验收
| ID | 场景与输入 | 期望 | 验证方式（计划，未运行） |
|---|---|---|---|
| A-1 | `pool_size>0` 的 profile 按 `a,b,a,b,a` 交替库调用 | 只创建 2 个池；实库上同一库的两次调用拿到同一个 `CONNECTION_ID()` | `tests/test_runtime.py` 假 pool factory 计数；`tests/test_local_readonly.py` 用 `test_db` 和不传库两种情况对比 `CONNECTION_ID()`（CI MySQL） |
| A-2 | 同一库下依次传 `timeout_ms=5000/30000/5000` | 不创建新池；connector 的 `read_timeout` 等于 profile 上限；会话里的 `MAX_EXECUTION_TIME` 仍等于本次调用的有效超时 | 单元测试检查 pool factory 参数和执行的 SET 语句 |
| A-3 | 访问的不同库数量超过 `max_database_pools` | 淘汰最久未用的池；被淘汰的池里正在用的连接归还时，底层连接被关闭，不会放回池里 | 单元测试：假连接记录 `close` 和归还调用 |
| A-4 | 密码轮换、隧道 generation 变化、`pool_size` 变化 | 该 profile 的全部池都退役，原有测试继续通过 | `tests/test_runtime.py` 现有的 pool 测试 |
| A-5 | 先用库 `a` 调用，再不传 database 调用（profile 也没有默认库） | 第二次调用使用「无库」池的连接，不会在 `a` 上执行 | 单元测试：断言两次使用不同的池；实库：不带库名的表查询报「No database selected」 |
| A-6 | profile A 的隧道启动卡在就绪等待中，同时直连 profile B 获取端点 | B 立即返回，不等 A | 单元测试：用 `threading.Event` 阻塞 A 的就绪检查，B 在短超时内拿到端点 |
| A-7 | 配置的 `local_port` 已经有程序在监听 | 不启动 ssh，直接报错，提示端口被占用 | 单元测试：本地先监听一个端口，断言 `popen_factory` 没被调用 |
| A-8 | 端口可以连上，但 ssh 进程在就绪判定时已经退出 | 报告隧道提前退出，不返回端点 | 单元测试：假进程的 `poll()` 返回退出码 |
| A-9 | `max_database_pools` 的配置与校验：TOML 和 `MYSQL_MAX_DATABASE_POOLS` 都能设置，超出 1–16 报错；`docs/CONFIGURATION.md` 的 `MYSQL_LOCAL_PORT` 默认值写成 3330 | 配置生效，文档与代码一致 | `tests/test_profiles.py` |

## 兼容、风险与回滚
- **超时语义（Q-2）**：单次调用的 `timeout_ms` 比 profile 上限小时，客户端照常在 `timeout_ms` 到期时收到 `QUERY_TIMEOUT`。SELECT 也会被服务器端的 `MAX_EXECUTION_TIME` 按时终止。SHOW、DESCRIBE、EXPLAIN 不受 `MAX_EXECUTION_TIME` 约束；在 C 扩展下，后台线程和准入名额可能一直占到 profile 上限的 socket 超时才释放。
- **空闲连接数**：每个池都会建满 `pool_size` 条连接，同一 profile 的空闲连接最多是 `max_database_pools × pool_size`（默认 15），要注意账号的 `max_user_connections`；可以调小 `max_database_pools` 或 `pool_size`。同时借出的连接数仍受 E-10 的准入上限约束。
- **端口预检**：旧单连接模式默认用 3330 端口（Q-3 保持不变），同一台机器上开两个启用 SSH 的服务进程时，第二个原来会悄悄复用第一个的隧道，修改后会报端口占用；需要多进程时为每个进程设置不同的 `MYSQL_LOCAL_PORT` 或设为 0。CHANGELOG 要写明。
- **私有属性**：退役判断需要读取 connector 池化连接的私有属性（如 `_cnx_pool`、`_cnx`）。现有代码已经在用 `_cnx_queue`（`src/mysql_mcp_server/runtime.py:217`），升级 connector 时要靠测试兜住。
- **残余风险**：E-17 提到的端口抢占时间窗。
- **回滚**：三个切片互相独立，可以分别回退提交；没有数据或配置格式迁移。

## 已裁决
| ID | 问题 | 裁决（2026-11-01） |
|---|---|---|
| Q-1 | 同一 profile 最多保留几个库的池 | 新增配置项 `max_database_pools`（见 E-12） |
| Q-2 | 能否接受 E-11 的超时语义变化 | 接受 |
| Q-3 | 旧单连接模式 `MYSQL_LOCAL_PORT` 默认值 | 保持 3330，改文档（见 E-18） |
