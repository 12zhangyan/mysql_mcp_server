// ─── 研发变更档案目录 ────────────────────────────────────────────────────────
// 方案与分析文档落盘后默认创建或更新交付档案；package/repair/loop 匹配到已有档案时续写同一 deliveryId，check 默认不写入。
// bug-fix、code-reading、biz-flow 保留各自记录类型；渲染逻辑在 js/board.js。
// 数据只能通过 board-add.js 写入；模板保持空目录。

const htmlChangelog = [
  {
    "date": "2026-10-19",
    "desc": "新增方案：查询热路径与可扩展性优化（7 项，S-7 待确认）"
  },
  {
    "date": "2026-10-19",
    "desc": "Q-1 已裁决（暂不扩展，取消 S-7）；S-1 至 S-6 已实施，未提交"
  },
  {
    "date": "2026-10-19",
    "desc": "A-6 本机 MySQL 8.4.0 实库验收通过；全部验收完成，待提交"
  },
  {
    "date": "2026-10-19",
    "desc": "用户裁决：MariaDB 不需要实库验证"
  },
  {
    "date": "2026-10-19",
    "desc": "PR #13 已合并到 main（02d1f8c），交付完成"
  },
  {
    "date": "2026-10-27",
    "desc": "新增方案：MCP 使用反馈优化（内置函数误拦与超时上限提示）"
  },
  // ─── 在此行上方追加变更日志 ───
];


// ─── 轻量目录数据 ────────────────────────────────────────────────────────────
// 首页、搜索和筛选只读取这里；人类方案正文位于 data/details/<detailId>.js，点击后加载。
// md 是 Agent 执行文档；看板详情是独立撰写的人类方案，不是 md 摘录。
// Agent 专属字段 changeList / todos / stackTrace / codeLocation 禁止进入目录和详情。
// 目录字段由 board-add.js 白名单控制；禁止手工整体重写本文件。
const changes = [
  {
    "updatedAt": "2026-10-20",
    "service": "mysql_mcp_server",
    "module": "runtime / server",
    "title": "查询热路径与可扩展性优化",
    "date": "2026-10-19",
    "status": "已完成",
    "docPath": "docs/2026-10-19/runtime-and-extensibility-optimization.md",
    "currentGate": "plan",
    "deliveryId": "DLV-A40235645F",
    "detailId": "d-a40235645fb01d62",
    "detailPath": "data/details/d-a40235645fb01d62.js",
    "summary": "已合并（PR #13）：每次请求最多取一次凭据，MySQL 会话设置减少一次往返，排队即时唤醒，审计写入提速约 2 倍，工具改为注册表分发；对外行为不变。",
    "searchText": "查询热路径与可扩展性优化 mysql_mcp_server runtime / server 已合并（PR #13）：每次请求最多取一次凭据，MySQL 会话设置减少一次往返，排队即时唤醒，审计写入提速约 2 倍，工具改为注册表分发；对外行为不变。 Done 每次查询都有可省掉的开销：重复读取凭据（使用 command 时每次查询启动一个子进程）、会话设置往返 3 次、排队靠 10ms 轮询、审计每条写入多次访问文件系统。同时 server.py 有 2700 多行，新增工具要改 3 处。 上一轮已把 SQL 解析合并为一次。这一轮处理代码中已确认的其余冗余和扩展障碍。 每次请求最多解析一次凭据，可选按连接配置开启短期缓存（默认关闭） MySQL 5.7.20 及以上版本的会话设置从 3 次往返减为 2 次 排队请求在名额释放时立即被唤醒 审计写入减少系统调用 工具定义和分发改为注册表；把审计和错误补全收拢成一个请求上下文 为数据库适配层抽出接口（待确认） 不抽数据库适配层（Q-1 裁决：暂不扩展，S-7 取消） 不跨请求缓存 SQL 或结果 不长期持有审计文件句柄 对外工具列表、错误码和审计字段不变 按 7 个切片推进，每个独立提交。凭据：请求内只解析一次，TTL 缓存默认关闭，遇到认证失败时清除。会话设置：根据握手阶段拿到的版本号判断，只有 MySQL 才合并 SET 语句，M"
  },
  {
    "updatedAt": "2026-10-27",
    "service": "mysql_mcp_server",
    "module": "sql_guard / server",
    "title": "MCP 使用反馈优化",
    "date": "2026-10-27",
    "status": "草稿",
    "docPath": "docs/2026-10-27/mcp-usage-feedback-optimization.md",
    "deliveryId": "DLV-134D78CF62",
    "currentGate": "plan",
    "detailId": "d-134d78cf62a0ad97",
    "detailPath": "data/details/d-134d78cf62a0ad97.js",
    "summary": "方案已生成，未实施：7 条使用反馈里只有 2 条需要改代码——放行 CRC32 等纯内置函数，以及让超时超限的报错说清楚上限来源。",
    "searchText": "MCP 使用反馈优化 mysql_mcp_server sql_guard / server 方案已生成，未实施：7 条使用反馈里只有 2 条需要改代码——放行 CRC32 等纯内置函数，以及让超时超限的报错说清楚上限来源。 Draft 一次跨库对账任务暴露了 7 个使用问题。逐条核对后，只有“内置函数被当成 UDF 拦截”和“超时超限提示不清”需要改本仓库；其余属于设计如此、连接配置，或发生在客户端和另一个服务。 对账时想用 CRC32 做两边数据的校验和，被函数白名单拒绝，只能退回行数、合计、长度。所有连接的 allowed_functions 都是空的。REPLACE 被拦的问题已在今天发布的 0.9.11 修复，推测当时用的是旧版本。 常见的纯内置函数（CRC32、CONV、ORD、FIND_IN_SET 等）在不配置 allowed_functions 时可用 函数被拦或 timeout_ms 超限时，报错直接说明改哪个配置项 sql_guard 的内置安全函数名单与报错文案 timeout_ms 超限报错与工具描述 README 增加常见问题小节 不放开 information_schema 直查，继续用 get_schema_info / inspect_catalog 不提高任何连接的超时上限，不改生产连接 honglian-prd 的库范围 不处理 Claude "
  },
  // ─── 在此行上方追加新记录 ───
];
