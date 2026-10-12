"""The MCP surface stays exactly four read-only tools."""

from __future__ import annotations

import asyncio

from fastmcp import Client

from tesla_fleet.server import mcp

EXPECTED = {"list_vehicles", "get_vehicle_status", "get_charging_history", "get_api_spend"}
COMMAND_WORDS = ("lock", "unlock", "honk", "flash", "climate_on", "start", "stop", "set_", "command", "call", "wake_vehicle")


def tool_names() -> set[str]:
    async def listed() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(listed())


def test_exactly_four_tools():
    assert tool_names() == EXPECTED


def test_no_command_or_passthrough_tools():
    for name in tool_names():
        assert not any(word in name for word in COMMAND_WORDS), name
