window.BOARD_DETAILS = window.BOARD_DETAILS || {};
window.BOARD_DETAILS["d-a71a7a353f144a78"] = {
  "delivery": {
    "plan": {
      "status": "Draft",
      "summary": "S-1 至 S-3 已实施，PR #31 待合并：README 从 650 行精简到约 160 行，配置、工具、排障、测试拆到 docs，新增 CLAUDE.md，CI 增加 MySQL 8.4。没有改动任何运行代码。",
      "background": "参考项目只有一个查询工具，写操作按环境变量开关。当前项目有 12 个工具、命名连接与路由、审计、凭据库、Bearer 鉴权和 AST 级脱敏。但当前 README 有 650 多行，配置、工具、测试、安全混在一起，没有 Claude Code 一行安装，固定版本示例还写着 0.9.1，仓库也没有 CLAUDE.md。",
      "goals": [
        "新用户首屏就能跑通：Claude Code 一行安装加最小配置",
        "配置、工具、排障、测试各有独立文档，README 变短",
        "贡献者和 Agent 有一份可直接执行的构建、测试、发版指引",
        "CI 的真实库验收覆盖用户实际使用的 MySQL 8.4"
      ],
      "scopeIn": [
        "README 拆分，新增 docs 下的配置、工具、排障、测试文档",
        "新增 CLAUDE.md",
        "CI 矩阵增加 MySQL 8.4"
      ],
      "scopeOut": [
        "不开放写操作，不加缓存、限流、查询复杂度评分，不拒绝 SELECT *",
        "不做 Unix socket、mTLS 客户端证书、时区选项、值级脱敏（暂缓，出现真实需求再做）",
        "不做 Smithery、Codex 插件打包",
        "不改任何运行代码和工具定义"
      ],
      "solution": "README 只保留特性、安装、最小配置和工具一览，细节原样迁移到 docs 下，README 内的文档链接用 GitHub 绝对地址，因为 npm 包里不含 docs。版本示例改成占位符，避免每次发版后过期。CI 只给一个组合增加 MySQL 8.4，不让任务数翻倍。",
      "decisions": [
        "不让发版脚本去改 README 里的版本号：多一个替换点，示例也不需要真实版本",
        "不把 docs 打进 npm 包：用绝对链接即可，不改包内容白名单",
        "写操作、缓存、限流等不采纳：违背只读定位，或已有更合适的机制",
        "mTLS、socket、时区、值级脱敏暂缓：当前 9 个连接都用不到"
      ],
      "acceptance": [
        "旧 README 的每个标题和代码块都能在新 README 或 docs 中找到",
        "README 中没有指向 docs 的相对链接，也没有写死具体版本的 npx 示例",
        "CLAUDE.md 里的命令在干净检出上都能运行",
        "CI 在 MySQL 8.0 和 8.4 上都通过真实库验收，断言未放宽"
      ],
      "assumptions": [
        "拆分后的文档对新用户确实更快上手：未做新用户试用",
        "8.4 上 CI 若暴露新的认证或种子数据问题，在同一切片里按 8.4 行为修正"
      ],
      "next": "合并 PR #31。暂缓项（Unix socket、mTLS、时区、值级脱敏）如需采纳，告诉我具体哪一项，单独出方案。"
    }
  }
};
