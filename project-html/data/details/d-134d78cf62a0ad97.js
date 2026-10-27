window.BOARD_DETAILS = window.BOARD_DETAILS || {};
window.BOARD_DETAILS["d-134d78cf62a0ad97"] = {
  "delivery": {
    "plan": {
      "status": "Draft",
      "summary": "一次跨库对账任务暴露了 7 个使用问题。逐条核对后，只有“内置函数被当成 UDF 拦截”和“超时超限提示不清”需要改本仓库；其余属于设计如此、连接配置，或发生在客户端和另一个服务。",
      "background": "对账时想用 CRC32 做两边数据的校验和，被函数白名单拒绝，只能退回行数、合计、长度。所有连接的 allowed_functions 都是空的。REPLACE 被拦的问题已在今天发布的 0.9.11 修复，推测当时用的是旧版本。",
      "goals": [
        "常见的纯内置函数（CRC32、CONV、ORD、FIND_IN_SET 等）在不配置 allowed_functions 时可用",
        "函数被拦或 timeout_ms 超限时，报错直接说明改哪个配置项"
      ],
      "scopeIn": [
        "sql_guard 的内置安全函数名单与报错文案",
        "timeout_ms 超限报错与工具描述",
        "README 增加常见问题小节"
      ],
      "scopeOut": [
        "不放开 information_schema 直查，继续用 get_schema_info / inspect_catalog",
        "不提高任何连接的超时上限，不改生产连接 honglian-prd 的库范围",
        "不处理 Claude Code 权限分类器无结果、ssh-mcp-server 连接失败",
        "不做数据库适配层，不做 MariaDB 实库验证"
      ],
      "solution": "把一批已核对过的“纯内置、无副作用”函数加入安全名单，未知函数、UDF、带 schema 前缀的调用仍需 allowed_functions 放行。超时超限仍然直接拒绝，但报错和工具描述写明上限来自该连接的 query_timeout_ms，可用 list_connections 查看。",
      "decisions": [
        "不静默截断超限的 timeout_ms：调用方会误以为用了 60 秒",
        "不关闭函数检查：写权限账号会越过只读边界",
        "不只靠用户逐个连接填 allowed_functions：9 个连接都要改，且把内置函数当成待审 UDF"
      ],
      "acceptance": [
        "空 allowed_functions 下，BIT_XOR(CRC32(CONCAT_WS(...))) 可通过校验",
        "未知函数和 app.crc32() 这类带前缀调用仍被拦，SLEEP、GET_LOCK、LOAD_FILE 仍被拒",
        "timeout_ms 超限时报错含 query_timeout_ms 与当前上限；工具快照只有 timeout_ms 描述变化",
        "完整 pytest 和 mypy 通过"
      ],
      "assumptions": [
        "反馈 2 里“REPLACE 被拦”来自 0.9.11 之前的版本；升级后仍复现则按新缺陷处理",
        "不带 schema 前缀的内置函数名总是解析到内置函数；若在 MySQL 8.4 上被推翻，则取消名单扩充，只保留报错文案",
        "UBP 注释乱码是库里的数据编码问题，不是 MCP 读取链路问题"
      ],
      "openQuestions": [
        "是否给 honglian-prd 增加 WMS、UBP 库的可见范围（配置层面，由你和 DBA 决定）",
        "是否把测试类连接的 query_timeout_ms 调到 60000 以上（配置层面）"
      ],
      "next": "评审方案；Q-1、Q-2 是配置建议，不阻塞实施。认可后可按 S-1、S-2、S-3 实施。"
    }
  }
};
