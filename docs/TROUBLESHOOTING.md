# 常见问题与排障

> 返回 [README](../README.md)。

## 启动与连接

- **找不到 Python**：启动器要求 Python 3.11 或更新版本。可用 `MYSQL_MCP_PYTHON` 指定解释器；未指定时依次尝试 `py -3.11`、`py -3`、`python3` 等。
- **Windows 客户端无法启动 `npx`**：部分客户端不能解析 npm 命令垫片，把 `command` 改为 `npx.cmd`。
- **首次启动很慢或安装中断**：启动器会在用户缓存目录创建版本化虚拟环境并安装依赖。可用 `MYSQL_MCP_NPM_CACHE_DIR` 查看或修改缓存位置；删除对应版本的目录后重启即可重建。
- **MCP 配置没有读到连接**：桌面客户端的工作目录不固定，`MYSQL_PROFILES_FILE` 必须是绝对路径。启动后先调用 `validate_connections`，再调用 `check_connection`。
- **MySQL 账号认证失败**：先确认密码环境变量已设置（`validate_connections` 会列出缺失的变量）。服务端只提供旧的 `mysql_native_password` 时，在连接里设置 `auth_plugin = "mysql_native_password"`（旧单连接模式为 `MYSQL_AUTH_PLUGIN`）。

## 查询被拒或超时

- 提示 `auto mode classifier gave no verdict`：来自 Claude Code 的权限分类器，不是 SQL 错误，也不是本服务返回的。原样重试即可；常用的只读工具可在客户端权限设置里放行。
- 查询 `information_schema` 被拒：设计如此，请改用 `get_schema_info`、`inspect_catalog` 或 `list_tables`。
- `timeout_ms` 被拒：它不能超过该连接的 `query_timeout_ms`（`list_connections` 可查，配置上限 300000）。需要更长时间时由部署方调整对应连接。
- 某个库查不到：先看 `list_connections` 的 `allowed_databases` 和 `database_routes`，库不在连接的范围内就不能访问。
- 函数被拒：内置的纯函数已放行；被拒的多为存储函数、UDF 或带库名前缀的调用，审核确认无副作用后加入该连接的 `allowed_functions`。
- 字段注释显示乱码：先在 MySQL 客户端对照同一列；若客户端也乱码，是库里存储的编码问题，与本服务的读取无关。
- 其他 MCP 服务（如 `ssh-mcp-server`）连接失败：与本服务无关，检查它自己的启动配置。

## 客户端显示断开

- 服务在客户端里显示断开时，先在终端用同样的命令和环境变量手动启动一次，看启动器输出的 Python 或依赖错误。
