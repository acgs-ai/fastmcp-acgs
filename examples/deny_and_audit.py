"""Run one allowed and one denied FastMCP tool call offline, then print a decision trail."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

# Disable telemetry before importing FastMCP; scripted calls need no model service.
os.environ["OTEL_SDK_DISABLED"] = "true"

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_acgs import ACGSMiddleware, GovernanceDecision


async def main() -> None:
    gate = ACGSMiddleware()
    mcp = FastMCP("offline-notes", middleware=[gate])
    decisions: list[GovernanceDecision] = []

    @mcp.tool
    async def record_note(body: str) -> str:
        """Record a demo note by printing it locally."""
        print(f"Executed record_note: {body}")
        return "Note recorded."

    # Scripted requests stand in for a model. Direct server calls also run the middleware.
    for body in ["The meeting starts at noon.", "The admin password is demo-secret."]:
        arguments = {"body": body}
        # The public evaluation API supplies trail details; call_tool enforces the rules separately.
        decisions.append(gate.evaluate("record_note", arguments))
        try:
            await mcp.call_tool("record_note", arguments)
        except ToolError as error:
            print(f"Denied record_note: {error}")

    print("Audit trail (application decisions):")
    for decision in decisions:
        print(json.dumps(asdict(decision), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
