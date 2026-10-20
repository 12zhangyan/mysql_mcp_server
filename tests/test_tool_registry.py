import json
from pathlib import Path

import pytest

from mysql_mcp_server import server
from mysql_mcp_server.server import call_tool, list_tools

SNAPSHOT = Path(__file__).parent / "snapshots" / "list_tools.json"


async def test_list_tools_matches_published_snapshot_byte_for_byte():
    tools = await list_tools()
    rendered = [tool.model_dump(mode="json", exclude_none=True) for tool in tools]

    # Serialized text (not dict equality) so key order is pinned too.
    assert json.dumps(rendered, ensure_ascii=False, indent=2) + "\n" == (
        SNAPSHOT.read_text(encoding="utf-8")
    )


async def test_every_published_tool_has_exactly_one_handler():
    names = [tool.name for tool in await list_tools()]

    assert sorted(names) == sorted(server._TOOL_HANDLERS)
    assert len(names) == len(set(names))


async def test_unregistered_tool_name_is_rejected():
    result = await call_tool("drop_everything", {})

    assert result.isError is True
    assert result.content[0].text == (
        "Error calling tool drop_everything: Unknown tool: drop_everything"
    )


def test_registry_self_check_rejects_missing_or_extra_handlers(monkeypatch):
    handlers = dict(server._TOOL_HANDLERS)
    handlers.pop("dump_schema")
    handlers["ghost"] = handlers["query"]
    monkeypatch.setattr(server, "_TOOL_HANDLERS", handlers)

    with pytest.raises(
        RuntimeError, match=r"missing=\['dump_schema'\] extra=\['ghost'\]"
    ):
        server._verify_tool_registry_matches_definitions()


def test_duplicate_handler_registration_is_rejected(monkeypatch):
    monkeypatch.setattr(server, "_TOOL_HANDLERS", dict(server._TOOL_HANDLERS))

    with pytest.raises(RuntimeError, match="Duplicate tool handler: query"):

        @server._tool("query")
        async def _duplicate(name, arguments, connection, database):
            return []
