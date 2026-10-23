# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The middleware over a real stdio transport (a subprocess), on both protocol eras."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from tests.fakes import LEAK, WEATHER, answer

ROOT = Path(__file__).resolve().parents[1]


def _transport() -> StdioTransport:
    return StdioTransport(command=sys.executable, args=["-m", "tests.stdio_server"], cwd=str(ROOT))


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_server(mode: str) -> None:
    async with Client(_transport(), mode=mode, elicitation_handler=answer("accept")) as client:
        with pytest.raises(ToolError, match="Blocked by ACGS rule ACGS-006"):
            await client.call_tool("send_email", LEAK)
        assert (await client.call_tool("get_weather", WEATHER)).data == "Sunny in Paris"
        assert (await client.call_tool("skip_audit", {"job": "nightly"})).data == "skipped nightly"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_stdio_server_declined_review(mode: str) -> None:
    async with Client(_transport(), mode=mode, elicitation_handler=answer("decline")) as client:
        with pytest.raises(ToolError, match="did not approve"):
            await client.call_tool("skip_audit", {"job": "nightly"})
