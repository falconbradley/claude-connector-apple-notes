"""Server-level tests: tool registration and a real stdio handshake.

These exist because the tool layer is the part that breaks silently when
the MCP SDK moves underneath us — the notestore/protobuf tests pass just
fine against a server module that no longer imports.
"""

import asyncio
import sys

import pytest

EXPECTED_TOOLS = {
    "append_to_note",
    "create_folder",
    "create_note",
    "get_note",
    "get_note_link",
    "get_selected_notes",
    "get_attachment",
    "get_stats",
    "list_folders",
    "list_note_attachments",
    "move_note",
    "open_note_in_notes",
    "read_table",
    "search_notes",
    "update_note",
}


def _list_tools():
    from apple_notes_mcp import server

    return asyncio.run(server.mcp.list_tools())


def test_server_module_imports():
    """Guards against SDK drift (e.g. FastMCP -> MCPServer in mcp 2.0)."""
    from apple_notes_mcp import server

    assert server.mcp is not None


def test_all_tools_registered():
    assert {t.name for t in _list_tools()} == EXPECTED_TOOLS


def test_every_tool_has_a_description_and_schema():
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"
        schema = tool.input_schema
        assert schema.get("type") == "object", f"{tool.name} schema not an object"


def test_write_tools_document_their_side_effects():
    """update_note overwrites; that has to be visible to the model."""
    tools = {t.name: t for t in _list_tools()}
    assert "OVERWRITES" in tools["update_note"].description
    assert "append_to_note" in tools["update_note"].description


def test_write_tools_warn_about_checklists():
    """The checklist refusal must be discoverable from the tool schema."""
    tools = {t.name: t for t in _list_tools()}
    for name in ("update_note", "append_to_note"):
        assert "checklist" in tools[name].description.lower(), name
        assert "force" in tools[name].input_schema["properties"], name


@pytest.mark.skipif(sys.platform != "darwin", reason="stdio launch is macOS-only")
def test_stdio_handshake():
    """Launch the real server over stdio and complete initialize."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from apple_notes_mcp.server import main; main()"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return {t.name for t in result.tools}

    assert asyncio.run(asyncio.wait_for(go(), timeout=60)) == EXPECTED_TOOLS
