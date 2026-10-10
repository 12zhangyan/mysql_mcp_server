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

按用户指定，使用当前工作区源码连接 test16.hl_eam，服务端 MySQL 8.4.6。11 项验收全部通过；原始记录包含 4 个源码文件 SHA-256，已确认与当前文件一致。连接目标越界尝试为 0，测试结束后并发占用为 0。

| 验收项 | 实测结果 |
| --- | --- |
| 当前源码与连通性 | 通过，非已安装旧包 |
| 会话只读与事务 | transaction_read_only=1，autocommit=0 |
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

- 报告：`.pytest_cache/test16-eam-live-1791617190295/report.json`
- 同目录 `verify.py` 为验收脚本，仅读取已配置的凭据到进程内存；报告不含凭据、连接串或业务数据行。
