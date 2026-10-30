# 开发与测试

> 返回 [README](../README.md)。以下内容从主 README 迁入，内容未改。

```bash
git clone https://github.com/12zhangyan/mysql_mcp_server.git
cd mysql_mcp_server
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt
pytest
npm test
```

调试 MCP 协议交互时可使用 MCP Inspector，不建议直接把 Python 进程当作普通命令行程序交互。

`tests/test_local_readonly.py` 是显式启用的本机验收测试。仅在进程环境设置 `MYSQL_MCP_LOCAL_TESTS=1`、`MYSQL_USER`、`MYSQL_PASSWORD` 后运行 `python -m pytest tests/test_local_readonly.py -q`；固定连接 `127.0.0.1:3306`，不使用远程配置或 SSH。测试使用 CTE 构造数据并读取已有元数据，不创建或修改表。覆盖 MySQL 8 的 CTE/UNION/嵌套查询/已有分页/窗口函数改写对照、中文注释搜索、响应预算续查和真实数据库错误。未显式启用时跳过；凭据不要写入文件。
