"""MCP wrapper: publishes the core as four read-only tools. No logic here."""

from __future__ import annotations

from fastmcp import FastMCP

from . import core

mcp = FastMCP("tesla-fleet")


@mcp.tool
def list_vehicles() -> list[dict]:
    """List vehicles on the account with their online/asleep state. Never wakes a car. Cheapest call; use it first."""
    return core.list_vehicles()


@mcp.tool
def get_vehicle_status(vin: str, wake: bool = False, include_location: bool = False) -> dict:
    """Battery %, range, charging state, charge limit, lock, climate and odometer for one vehicle.

    If the car is asleep and wake=False, returns its state without waking it. Only set wake=True
    when the user explicitly wants live data from a sleeping car: a wake costs about 10x a read.
    Location is returned only when include_location=True.
    """
    return core.vehicle_status(vin, wake=wake, include_location=include_location)


@mcp.tool
def get_charging_history(start: str, end: str) -> dict:
    """Supercharger sessions and cost between two dates (YYYY-MM-DD). Excludes home charging."""
    return core.charging_history(start, end)


@mcp.tool
def get_api_spend(month: str | None = None) -> dict:
    """Fleet API requests made this month (or YYYY-MM) and estimated spend vs the monthly credit. No Tesla call."""
    return core.spend_ledger(month)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
