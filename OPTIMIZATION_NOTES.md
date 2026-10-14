# 查询诊断优化参考与边界

参考日期：2026-10-10。以下是设计对照，不代表同类项目完整评测；本轮未升级依赖或修改发布版本。

| 参考实现或文档 | 查阅到的做法 | 本项目处理 |
| --- | --- | --- |
| [Bytebase DBHub：explain_sql](https://github.com/bytebase/dbhub/blob/main/docs/tools/explain-sql.mdx) | 独立计划工具；服务添加 EXPLAIN 前缀，拒绝输入自带 EXPLAIN/ANALYZE | 新增 explain_sql；维持本项目更严格的只读边界，只接收 SELECT/WITH，不开放写语句的计划 |
| [Bytebase DBHub：search_objects](https://github.com/bytebase/dbhub/blob/main/docs/tools/search-objects.mdx) | 按名称/摘要/详情逐步发现元数据 | 现有 list_tables 搜索、get_schema_info detail、dump_schema 和完整分页已覆盖本轮需求，保留现有入口 |
| [jakubpliszka/mysql-mcp-server](https://github.com/jakubpliszka/mysql-mcp-server) | 将只读会话、服务端超时和客户端限制组合使用 | 保留已有多层限制，补上优化器提示覆盖限制的校验及超时的可操作错误分类 |
| [MySQL 优化器提示](https://dev.mysql.com/doc/refman/8.4/en/optimizer-hints.html) | SET_VAR 可暂时改会话变量；MAX_EXECUTION_TIME 可覆盖会话超时；RESOURCE_GROUP 可改变资源组 | 在扫描真正的优化器注释时拒绝这三类提示；字符串、普通注释和普通计划提示不因此被拒绝 |
| [MySQL EXPLAIN](https://dev.mysql.com/doc/refman/8.4/en/explain.html) | 普通计划与实际运行的 EXPLAIN ANALYZE 有不同语义 | 只生成普通 EXPLAIN；不对原 SQL 下推分页，不把估算当作真实耗时 |
| [MySQL 服务端错误](https://dev.mysql.com/doc/mysql-errors/8.0/en/server-error-reference.html)、[客户端错误](https://dev.mysql.com/doc/mysql-errors/8.4/en/client-error-reference.html)、[MariaDB 1969](https://mariadb.com/docs/server/reference/error-codes/mariadb-error-codes-1900-to-1999/e1969) | 区分语句超时、锁等待、连接失败和连接中断 | 输出固定安全消息、实际目标、错误阶段和恢复建议；不回传驱动原文，不因标记可重试就自动重放 |

实现为本项目独立编写，未引入参考项目代码或新依赖。现有 execute_sql/query 参数兼容；截止时间错误由旧的普通错误文本改为结构化 QUERY_TIMEOUT，依赖旧文本格式的调用方应读取 code/message。

单元验证采用既有测试、行为回归和 MCP STDIO 握手/工具调用，其数据库适配器为 mock。另已按用户授权在 test16.hl_eam 执行真实 MySQL 验收，结果见下文；localhost 测试仍通过 MYSQL_MCP_LOCAL_TESTS 显式启用。验收通过不等同于生产性能提升。

## 复核后的三项修复

- SQL 解析失败统一为 SQL_PARSE_ERROR；仅取 SQLGlot 结构化错误中的数字行列，不使用 description、上下文、highlight 或异常字符串。
- 使用 Connector/Python 的实际 ReadTimeoutError / WriteTimeoutError 类型识别 socket 超时，禁止自动重放并关闭活动连接；原有兼容重试只保留给 execute/fetch 阶段的 InterfaceError(errno=-1)。
- explain_sql 默认不切断单元格；超预算时明确提示 plan_output=chunks。分片固定请求 JSON 计划，先脱敏后拆分，沿用页预算与 next_offset；续页需 expected_plan_id，内容或上下文变化会返回 PLAN_CHANGED。普通数据查询仍保留 max_cell_length 限制。

新增回归使用真实驱动异常类型，并覆盖解析字面量保密、连接清理、长 JSON/TREE 单元格、中文/emoji 分片重组、响应字节预算、脱敏顺序、续页指纹变化及 MCP STDIO 协议。共享数据库的超时/断连未主动制造，继续以故障注入覆盖。已完成的 MySQL 实库验收见下文；MariaDB 尚未实测。

## test16.hl_eam 真实只读验收（2026-10-10）

按用户指定，使用当前工作区源码连接 test16.hl_eam，服务端 MySQL 8.4.6，使用与 CI 一致的 SQLGlot 30.22.0。12 项验收全部通过；原始记录包含 4 个源码文件 SHA-256，已确认与当前文件一致。连接目标越界尝试为 0，测试结束后并发占用为 0。

| 验收项 | 实测结果 |
| --- | --- |
| 当前源码与连通性 | 通过，非已安装旧包 |
| 会话只读与事务 | transaction_read_only=1，autocommit=0 |
| CTE/UNION 字段来源脱敏 | 2 行常量结果的敏感来源列经别名后仍隐藏，普通状态列保留；真实 STDIO 同样通过 |
| EAM 元数据与分页 | 检查 2 页共 6 张表、4 行列信息和 4 行详细元数据 |
| EAM 真实表执行计划 | 返回 1 行普通 EXPLAIN；未执行该业务表 SELECT |
| 常量/CTE 字节预算分页 | 6 页完整取得 6 条合成记录，无重复或丢失 |
| 解析错误保密 | SQL_PARSE_ERROR，不回显测试字面量，数据库连接次数为 0 |
| 真实数据库异常 | COLUMN_NOT_FOUND、TABLE_NOT_FOUND 分类正确，各执行 1 次 |
| 覆盖限制的提示及 ANALYZE 输入 | 2 项在连接前拒绝 |
| 长 JSON 计划 | 24,215 字符，95 片，8 页，最大单页 4,041 字节；重组可解析且总长度一致 |
| 计划指纹校验 | 不匹配返回 PLAN_CHANGED，不交付片段 |
| MCP STDIO → test16.hl_eam | 当前源码暴露 12 个工具；真实查询、错误返回和分片重组通过 |

未执行 DML、DDL、ANALYZE、全局变量或权限变更；没有修改业务数据。现有账号具有非只读权限，本轮确认的是服务 SQL 校验与只读会话生效，不代表数据库账号已收敛为 SELECT-only。驱动超时/断连、MariaDB 兼容性和生产负载效果不属于本次实测结论。

本地可复核记录：

- 报告：`.pytest_cache/test16-eam-pr-final-1791618627298/report.json`
- 同目录 `verify.py` 为验收脚本，仅读取已配置的凭据到进程内存；报告不含凭据、连接串或业务数据行。

## SQLGlot 兼容性验证

SQLGlot 30.22.0 将 lineage() 的 schema 改为必填参数。三处字段来源追踪调用显式传入空 schema，保留对已有版本的兼容性，避免异常回退导致 CTE 别名字段漏脱敏。回归覆盖投影数量不匹配、普通 CTE、嵌套 CTE 和 UNION，并确认普通列未被误隐藏。

SQLGlot 30.14.0 与 30.22.0 下的完整 Python 测试分别为 357 passed、16 skipped；跳过项为显式启用的 localhost 验收。mypy 检查 12 个源码文件通过。以上最终源码已重新完成前述 12 项 test16.hl_eam 只读验收。


## 本地多列查询与连接池优化（2026-10-14）

已复现并修复两项问题：

- 字段脱敏追踪原来为每个输出列重复准备整条 SQL，并复制用于图示的 AST。现在每次请求只准备一次字段分析作用域，逐列追踪时复用该作用域；保留原有 CTE/UNION、重复列名、JSON 脱敏和分析失败回退规则。不跨请求缓存 SQL 或结果，不增加依赖。
- 连接池缓存键原来没有包含 pool_size。实库中将池容量从 1 调为 2 后，第二个借用仍抛 PoolError；现在容量变化会重建池。增长、缩小、替换后复用以及旧配置清理不误删新池均有回归覆盖。

测量环境为 localhost MySQL 8.4.0、Python 3.14.5、SQLGlot 30.14.0，使用当时的非空脱敏规则和直接连接（pool_size=0）。这些计时来自脱敏开启的路径；当前默认关闭脱敏，复跑该路径需显式配置字段。每个宽度执行 3 次，取中位数；优化前后顺序运行同一个基准，预先完成连接预热，并逐次检查敏感列已隐藏、普通列保持原值。基线加载修改前的字段追踪实现，其余查询路径与优化后相同。

下表计时从调用 execute_query 开始，覆盖安全校验、数据库访问、脱敏和响应预算处理；不包含 MCP STDIO 传输。查询均为单行合成 CTE，不读取业务数据行。

| 返回列数 | 优化前查询处理耗时 | 优化后查询处理耗时 | 加速比 |
| --- | --- | --- | --- |
| 25 | 70.763 ms | 16.804 ms | 4.2 倍 |
| 100 | 1000.256 ms | 42.056 ms | 23.8 倍 |
| 200 | 3760.664 ms | 114.345 ms | 32.9 倍 |

200 列的纯脱敏追踪中位耗时由 3698.066 ms 降为 67.113 ms。以上只证明本机合成多列查询的改善，不能推断生产负载下的吞吐量或响应时间。

验证结果：

- 默认环境完整 Python 回归：367 passed、20 skipped；跳过的 20 项是需显式开启的本地实库验收。
- 本地 MySQL 只读验收：20 passed，包含多列 CTE/UNION、真实 MCP STDIO 脱敏、连接池扩缩容和原有分页/元数据/错误恢复用例。
- SQLGlot 30.22.0 完整回归加本地验收：387 passed；SQLGlot 26.0.0 相关脱敏与连接池回归：32 passed。版本验证分别使用隔离的依赖目录，没有修改项目依赖范围。
- npm test：6 passed；mypy：12 个源码文件通过；修改过的 6 个 Python 文件通过 Black 检查，compileall 和 git diff --check 通过。
- 实库连接确认 transaction_read_only=1、autocommit=0；没有执行 DML/DDL 或权限变更。测试凭据仅通过子进程环境传入，未写入脚本、报告或项目配置。

复跑基准：先在临时进程环境提供 MYSQL_USER、MYSQL_PASSWORD 并设置 MYSQL_MCP_LOCAL_TESTS=1，然后运行：

`python scripts/benchmark_local_readonly.py --iterations 3 --columns 25 100 200 --mask-sensitive-field`

不带 `--mask-sensitive-field` 时按默认不脱敏模式运行；报告中的 `mask_columns` 记录本次实际启用的规则。

脚本强制使用 127.0.0.1:3306，清除继承的连接 profile、SSH、凭据提供器和其他 MySQL 设置，仅执行合成只读查询并输出版本与耗时。实库专用的 MYSQL_SSL_MODE=DISABLED、MYSQL_AUDIT_ENABLED=false 等环境变量应局限在实库 fixture 或基准子进程中；不要把它们带入依赖默认安全策略的完整单元测试环境。

当前本地原始记录保存在 .pytest_cache/optimization-20261014/controlled-before.json、controlled-after.json、local-optimized.json 和 sqlglot30-clean.json；这些是可清理的本地验证记录，不随发布包分发。

## 脱敏由配置控制，默认全部放开（2026-10-14）

旧内置规则曾把业务字段和统计值误隐藏。现在移除内置敏感字段名单，默认 `mask_columns=()`：命名连接省略 `mask_columns` 或设为 `[]`、旧单连接模式省略或清空 `MYSQL_MASK_COLUMNS`，均返回原始字段值。JSON 内键同样默认不脱敏。

需要隐藏的字段全部由配置决定，支持字段名、通配符和限定路径。已有非空配置继续生效，不自动合并任何内置名单；命名连接不会叠加旧单连接环境变量里的规则。README、TOML、环境变量及企业部署示例均已同步。

显式启用脱敏后，不带点的模式仅匹配字段名，避免表名牵连普通列；`COUNT` 和 `COUNT(DISTINCT ...)` 返回统计值，匹配规则的原始值和拼接值仍会隐藏。别名、CTE/UNION 和 JSON 的来源追踪继续生效。默认规则为空时跳过字段追踪和 JSON 脱敏，保留原始结果。

本轮最终验证：

- `python -m pytest tests -q`：410 passed、28 skipped；跳过项为需要显式开启的本地实库验收。
- `python -m pytest tests/test_local_readonly.py -q`：本地 MySQL 8.4.0 上 28 passed，包含默认放开/显式脱敏两种真实 STDIO 场景、多列 CTE/UNION、原始字段和嵌套 JSON。
- 执行计划分块回归分别验证默认保留和显式脱敏，命名连接及环境变量分别验证省略、空列表和自定义规则。
- `npm test`：6 passed；`mypy src`：12 个源码文件通过；9 个变动 Python 文件通过 Black 检查，compileall 和 `git diff --check` 通过。
- 本地基准脚本分别以默认模式和 `--mask-sensitive-field` 运行 25 列、1 次的功能检查，两者均成功。MySQL 确认 `transaction_read_only=1`、`autocommit=0`；该单次计时不作为性能对比结论。
- 仅使用合成 SELECT/CTE 数据及已有元数据，没有 DML/DDL。连接凭据只通过临时子进程环境传入，未写入配置、日志、脚本或报告。

本轮原始验证记录位于 `.pytest_cache/masking-default-open-20261014/`，不随发布包分发。本次完成源码与配置示例修改，尚未发布新包或替换已安装的 MCP。
