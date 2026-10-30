# 参考 benborla/mcp-server-mysql 的优化 开发方案

> 状态：S-1 至 S-3 已实施，PR #31 待合并
> 日期：2026-10-27 ｜ 基线：main@5c7c128 ｜ 来源：用户要求参考 https://github.com/benborla/mcp-server-mysql 优化当前项目，先写文档
> 实施范围：`README.md`、新增 `docs/` 下的说明文档、新增 `CLAUDE.md`、`.github/workflows/test.yml`；不改 `src/`、不改对外工具定义、不改发布流程

## 执行卡片

- 改变：① README 从 650 行的单文件拆成短入口加 `docs/` 说明，补上 Claude Code 一行安装和排障文档，去掉写死的旧版本号；② 新增面向贡献者和 Agent 的 `CLAUDE.md`；③ CI 的真实库验收增加 MySQL 8.4
- 保持：全部行为代码和工具契约；README 里现有的每一段说明都能在 README 或 `docs/` 找到；npm 包 `files` 白名单和发布流程
- 先核验：用 `git diff --stat` 确认实施时 `src/` 无改动；拆分前后对比二级、三级标题清单，缺项即停
- 首个切片：S-1
- 完成判据：A-1 至 A-5 通过；CI 全绿

## 背景与目标

- 背景：参考项目能力基本是当前项目的子集（E-1），值得借鉴的是入门体验和文档组织（E-2、E-3），以及缺少的贡献者指引（E-4）。能力层面的差异多数不该采纳（见方案中的对照表）。
- 目标：新用户 5 分钟内跑通；排障有独立入口；贡献者和 Agent 一眼知道怎么构建、测试、发版；CI 覆盖用户实际使用的 MySQL 版本。
- 非目标：
  - 不开放写操作，不加结果缓存，不加限流，不加查询复杂度评分，不加 `SELECT *` 或引用敏感列的拒绝规则
  - 不做 Unix socket、客户端证书（mTLS）、时区选项、值级脱敏（暂缓，见 E-6）
  - 不做 Smithery、Codex 插件打包
  - 不做数据库适配层，不做 MariaDB 实库验证（用户 2026-10-19 裁决）

## 证据与决策

| ID | 结论 | 证据 | 类型 |
|---|---|---|---|
| E-1 | 参考项目是 Node.js 实现，只有一个 `mysql_query` 工具和一个 `mysql://tables` 资源；写操作按环境变量开关；其 CHANGELOG 的“计划功能”（分页、超时、执行计划、日志、外键与索引信息、健康检查等）当前项目均已实现。当前项目有 12 个工具、命名连接与路由、审计、HMAC、凭据库、Bearer 鉴权与主机白名单、AST 级脱敏 | `gh api repos/benborla/mcp-server-mysql`（2186 星，最近推送 2026-07-27）；其 `README.md`、`CHANGELOG.md`、`docs/CONFIGURATION.md`；本仓库 `src/mysql_mcp_server/tool_specs.py`、`src/mysql_mcp_server/transport.py:270` | 已证实 |
| E-2 | 参考项目 README 约 100 行，只放特性、一行安装、工具列表和文档链接，配置、排障、PII、测试分别放在 `docs/`；当前 README 652 行，配置、工具、客户端、测试、安全混在一份里，npm 包只带 README，没有任何独立文档 | 参考项目 `README.md`、`docs/`；本仓库 `README.md:85`、`README.md:334`、`README.md:585`、`package.json:20` | 已证实 |
| E-3 | 当前 README 缺 Claude Code 一行安装（参考项目首屏就是 `claude mcp add`）；固定版本的示例写死 `@0.9.1`（当前为 0.9.12），共 5 处，发版脚本不维护它 | `README.md:47`、`README.md:61`、`README.md:515`、`README.md:535`、`README.md:560`；`grep -rn "claude mcp add" README.md ENTERPRISE_DEPLOYMENT.md MCP_USECASES.md` 无命中 | 已证实 |
| E-4 | 仓库没有 `CLAUDE.md` 或同类贡献者指引；构建、测试、发版命令分散在 `README.md:585`、`RELEASING.md`、`pytest.ini`、CI 配置里 | `git ls-files` 无 `CLAUDE.md`；`RELEASING.md`；`.github/workflows/test.yml` | 已证实 |
| E-5 | CI 的真实库验收固定用 `mysql:8.0`，而用户本机和 E-5（2026-10-27 方案）的验证都在 8.4.0；8.4 默认禁用 `mysql_native_password`，与 8.0 的认证行为不同 | `.github/workflows/test.yml:16`、`.github/workflows/test.yml:116`；`docs/2026-10-27/mcp-usage-feedback-optimization.md`（E-5） | 已证实；决策：把 MySQL 版本加入矩阵，只对一个组合跑 8.4，避免任务数翻倍（Agent 代决，错了的代价：多 1 个 CI 任务） |
| E-6 | 暂缓的能力：Unix socket（`MYSQL_SOCKET_PATH`）、mTLS 客户端证书（`MYSQL_SSL_CERT/KEY`）、时区与日期字符串选项、值级 PII 脱敏（邮箱、手机号、卡号正则）。理由：当前没有一个已配置的连接需要它们（9 个连接均为 TCP 加密码，`mask_columns` 均为空，日期已按 ISO 字符串输出）；与“无具体目标不扩展”的既有裁决一致。会推翻暂缓的前提：出现需要 socket 或客户端证书的真实部署，或需要对未知列名做兜底脱敏的合规要求 | `list_connections` 返回；`src/mysql_mcp_server/config.py:210`（只有 `auth_plugin`，无 socket、证书字段）；`src/mysql_mcp_server/results.py:45` | 已证实现状；暂缓为 Agent 代决 |
| E-7 | 不采纳：写操作开关（违背只读定位，见 `SECURITY.md`）；结果缓存（2026-10-19 方案已列为非目标）；每分钟限流（已有并发与排队准入，stdio 场景无多租户）；查询复杂度评分（已有超时、行数与响应字节预算）；`SELECT *`/敏感列引用一律拒绝（当前做法是按别名、CTE、JSON 键脱敏而不是拒绝，拒绝会误伤合法查询） | `docs/2026-10-19/runtime-and-extensibility-optimization.md`；`README.md:607`；`src/mysql_mcp_server/results.py:770` | 已否决替代 |
| E-8 | 拆分后的 README 在 npm 页面显示，但包里没有 `docs/`，相对链接会失效。决策：README 内指向 `docs/` 的链接一律写 GitHub 绝对地址（`https://github.com/12zhangyan/mysql_mcp_server/blob/main/docs/...`），不改 `files` 白名单 | `package.json:20`（`files` 只含 `README.md`、`ENTERPRISE_DEPLOYMENT.md`、`SECURITY.md`） | Agent 代决（错了的代价：npm 页面上少数链接指向 main 分支的最新文档，可能与已发布版本略有出入） |
| E-9 | 版本示例的处理：README 里不再写具体版本号，改成“固定到经过审核的版本：`npx -y @yanzhang123/readonly-db-mcp@<版本>`”。被否决的替代：让 `scripts/set-version.cjs` 同步改 README。理由：多一个需要维护和校验的替换点，且示例并不需要真实版本号。会推翻它的前提：用户希望示例可直接复制运行 | `scripts/set-version.cjs`、`scripts/verify-versions.cjs` | 已否决替代；Agent 代决 |

## 方案

### 与参考项目的对照

| 参考项目的做法 | 当前项目 | 结论 |
|---|---|---|
| README 短入口加 `docs/` | 单文件 650 行 | 采纳（S-1） |
| `claude mcp add` 一行安装 | 无 | 采纳（S-1） |
| `docs/TROUBLESHOOTING.md` | 只有 README 末尾新增的常见问题一节 | 采纳并扩充（S-1） |
| `CLAUDE.md` | 无 | 采纳（S-2） |
| CI 带 MySQL 服务 | 已有，仅 8.0 | 补 8.4（S-3） |
| 写操作开关、缓存、限流、复杂度评分、拒绝 `SELECT *` | 不具备 | 不采纳（E-7） |
| Unix socket、mTLS、时区、值级脱敏 | 不具备 | 暂缓（E-6） |
| Smithery、Codex 插件、`evals.ts` | 无 | 不做（非目标） |
| 单工具加 `mysql://tables` 资源 | 12 个工具、完整资源与提示词 | 无需借鉴 |

### S-1 文档重组

- `README.md` 保留：特性、环境要求、安装（npx 与 Claude Code 一行命令）、最小 MCP 配置、工具一览与链接、许可证。目标是显著短于现在，具体行数不设硬指标，以“首屏能跑通”为准。
- 新增 `docs/CONFIGURATION.md`：命名连接、凭据来源、逻辑路由、旧版环境变量、传输与 SSH、结果脱敏（现 `README.md:85` 至 `README.md:333`）。
- 新增 `docs/TOOLS.md`：各工具的参数、行为和可纠正的错误码（现 `README.md:334` 至 `README.md:494`）。
- 新增 `docs/TROUBLESHOOTING.md`：现有“常见问题”扩充，覆盖 PATH 找不到 Python、MySQL 8 认证插件（`auth_plugin`）、权限分类器无结果、`information_schema` 被拒、超时上限、函数被拒、库不在范围内。
- 新增 `docs/TESTING.md`：现 `README.md:585` 起的开发与测试。
- 安全设计与安全最佳实践保留在 README 或指向 `SECURITY.md`，不下沉。
- 内容原样迁移，不改措辞和行为描述；固定版本的写法按 E-9 统一；README 内指向 `docs/` 的链接按 E-8 写绝对地址。
- 具体文件划分由执行 Agent 微调，硬边界是不丢内容（A-1）。

### S-2 CLAUDE.md

写面向 Agent 和贡献者的简短指引：项目概述（只读 MySQL MCP，Python 加 npm 启动器）、目录结构、构建与测试命令（`pytest`、`mypy`、`npm test`、`npm run test:install` 及其前置的 wheel 构建）、发版步骤指向 `RELEASING.md`、安全边界（不放宽只读闸门）、已有裁决（仅 MySQL）。命令必须在仓库里实际跑通。

### S-3 CI 加 MySQL 8.4

`services.mysql.image` 改为取矩阵变量，矩阵仅为 Python 3.12 加 `latest` sqlglot 的组合增加 `mysql: 8.4`，其余保持 8.0。若 8.4 下种子表或认证步骤失败，在同一切片里按 8.4 的行为修正，不放宽测试断言。

## 工作切片

| 切片 | 可观察结果 | 改动范围 | 硬边界 | 依赖 | 验收 |
|---|---|---|---|---|---|
| S-1 | README 精简，`docs/` 有配置、工具、排障、测试四份文档，首屏有 Claude Code 一行安装 | `README.md`、`docs/CONFIGURATION.md`、`docs/TOOLS.md`、`docs/TROUBLESHOOTING.md`、`docs/TESTING.md` | 不改 `src/` 和 `package.json`；内容不丢；不改任何行为描述 | 无 | A-1, A-2, A-3 |
| S-2 | 仓库根有 `CLAUDE.md`，命令可直接复制运行 | `CLAUDE.md` | 不写密钥和个人路径；不复述 README 全文 | S-1（链接目标） | A-4 |
| S-3 | CI 在 MySQL 8.0 和 8.4 上都跑真实库验收 | `.github/workflows/test.yml` | 不放宽测试，不增加默认矩阵规模以外的任务 | 无 | A-5 |

切片顺序与粒度是建议；执行 Agent 可合并或重排，但不得越过硬边界。

## 验收

| ID | 场景与输入 | 期望 | 必须保持 | 验证方式（计划，未运行） |
|---|---|---|---|---|
| A-1 | 拆分前后对比 README 的二级、三级标题和代码块 | 旧 README 的每个标题与代码块都能在新 README 或 `docs/` 找到，内容一致 | `git diff --stat` 显示 `src/`、`tests/`、`package.json` 无改动 | 用脚本提取旧版（`git show HEAD:README.md`）与新文档的标题清单求差集，预期为空 |
| A-2 | 检查链接 | `docs/` 内相对链接全部存在；README 指向 `docs/` 的链接为 GitHub 绝对地址 | npm 页面不出现指向包内不存在文件的相对链接 | `grep -n "](docs/" README.md` 预期无命中；对 `docs/*.md` 内的相对链接逐个检查文件存在 |
| A-3 | 搜索写死版本号 | README 与 `docs/` 中不再出现 `@0.9.1` 等具体版本的 `npx` 示例；Claude Code 一行命令可复制 | 其余命令示例不变 | `grep -rn "readonly-db-mcp@[0-9]" README.md docs` 预期无命中；人工核对一行安装与 `.mcp` 配置示例的包名、参数 |
| A-4 | 逐条执行 `CLAUDE.md` 中列出的构建、测试命令 | 全部可运行并通过 | 不含密钥、本机绝对路径 | 在干净检出上按文档执行；`grep -n -i "password\|token\|C:\\\\Users" CLAUDE.md` 预期无敏感值 |
| A-5 | 提交 PR 后看 CI | 矩阵包含 `mysql: 8.4` 的组合并通过；其余组合不变 | 真实库验收测试未被跳过、断言未放宽 | `gh pr checks`；日志中 `tests/test_local_readonly.py` 的用例数与 8.0 组合一致 |

另需：完整 `pytest` 与 `mypy` 结果不变（本方案不动代码，仅作回归确认）。

## 兼容、风险与回滚

- 兼容：只动文档与 CI，对运行行为零影响。npm 页面上的 README 会变短，细节链接指向 GitHub 的 `main` 分支。
- 风险：拆分时漏迁内容（A-1 兜底）；npm 页面链接与已发布版本的文档略有出入（E-8）；8.4 上 CI 可能暴露此前未见的认证或种子数据问题（S-3 就地修正）。
- 回滚：三个切片互相独立，可单独回退；文档拆分可整体 `git revert`。
- 未覆盖：不验证 npm 页面的实际渲染；README 精简后是否真的“5 分钟跑通”未做新用户试用。
