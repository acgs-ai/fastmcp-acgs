# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests through an in-memory FastMCP client, on both protocol eras."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from acgs_lite import Constitution
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fastmcp_acgs import ACGSMiddleware, GovernanceDecision, Verdict
from tests.fakes import LEAK, REFUND, WEATHER, Ran, governed, review_rules, server

MODES = ["auto", "legacy"]  # "auto" negotiates 2026-07-28 with an in-memory server; "legacy" pins 2025-11-25


@pytest.fixture(params=MODES)
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def test_allowed_call_runs_unchanged(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran), mode=mode) as client:
        result = await client.call_tool("get_weather", WEATHER)
    assert result.data == "Sunny in Paris"
    assert ran.calls == [("get_weather", WEATHER)]


async def test_blocked_call_does_not_run_and_the_client_gets_the_reason(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran), mode=mode) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("send_email", LEAK)
        raw = await client.call_tool_mcp("send_email", LEAK)
    assert str(raised.value) == (
        "Tool call 'send_email' was not executed. "
        "Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses)."
    )
    assert raw.is_error
    assert "hunter2" not in str(raised.value)
    assert ran.calls == []


async def test_review_is_denied_by_default(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran), mode=mode) as client:
        with pytest.raises(ToolError, match="No reviewer is configured for this server, so it was denied"):
            await client.call_tool("issue_refund", REFUND)
    assert ran.calls == []


async def test_the_session_continues_after_a_denial(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran), mode=mode) as client:
        with pytest.raises(ToolError):
            await client.call_tool("send_email", LEAK)
        await client.call_tool("get_weather", WEATHER)
    assert ran.names == ["get_weather"]


async def test_masked_error_details_still_show_the_reason(mode: str) -> None:
    ran = Ran()
    mcp = server(ran, middleware=[ACGSMiddleware()], mask_error_details=True)
    async with Client(mcp, mode=mode) as client:
        with pytest.raises(ToolError, match="Blocked by ACGS rule ACGS-006"):
            await client.call_tool("send_email", LEAK)
    assert ran.calls == []


async def test_missing_arguments_are_evaluated_as_empty(mode: str) -> None:
    ran = Ran()
    mcp = server(ran, middleware=[ACGSMiddleware()])

    @mcp.tool
    def skip_audit() -> str:
        """Pretend to skip the audit."""
        ran.calls.append(("skip_audit", {}))
        return "skipped"

    async with Client(mcp, mode=mode) as client:
        with pytest.raises(ToolError, match="ACGS-002"):
            await client.call_tool("skip_audit")
    assert ran.calls == []


async def test_an_unknown_tool_is_evaluated_first_and_then_fails_normally(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran), mode=mode) as client:
        with pytest.raises(ToolError, match="Blocked by ACGS rule ACGS-006"):
            await client.call_tool("no_such_tool", LEAK)
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("no_such_tool", WEATHER)


async def test_the_tools_own_errors_pass_through(mode: str) -> None:
    mcp = server(Ran(), middleware=[ACGSMiddleware()])

    @mcp.tool
    def broken() -> str:
        """Always fails."""
        msg = "the tool failed"
        raise ToolError(msg)

    async with Client(mcp, mode=mode) as client:
        with pytest.raises(ToolError, match="the tool failed"):
            await client.call_tool("broken")


async def test_in_process_call_tool_is_governed() -> None:
    ran = Ran()
    mcp = governed(ran)
    with pytest.raises(ToolError, match="ACGS-006"):
        await mcp.call_tool("send_email", LEAK)
    result = await mcp.call_tool("get_weather", WEATHER)
    assert result.structured_content == {"result": "Sunny in Paris"}
    assert ran.names == ["get_weather"]


async def test_add_middleware_works_like_the_constructor_argument() -> None:
    ran = Ran()
    mcp = server(ran)
    mcp.add_middleware(ACGSMiddleware())
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="ACGS-006"):
            await client.call_tool("send_email", LEAK)
    assert ran.calls == []


def test_evaluate_does_not_run_anything() -> None:
    guard = ACGSMiddleware(constitution=review_rules())
    decision = guard.evaluate("issue_refund", REFUND)
    assert isinstance(decision, GovernanceDecision)
    assert decision.verdict is Verdict.REVIEW
    assert decision.rule_ids == ("REFUND-REVIEW",)


def test_defaults_and_repr() -> None:
    guard = ACGSMiddleware()
    assert guard.agent_id == "fastmcp"
    assert guard.on_review == "deny"
    assert guard.review_timeout == 300.0
    assert guard.constitution.name == Constitution.default().name
    assert repr(guard) == (
        f"ACGSMiddleware(agent_id='fastmcp', on_review='deny', constitution={Constitution.default().name!r})"
    )


@pytest.mark.parametrize("on_review", ["allow", "confirmed", "", None])
def test_invalid_on_review_is_rejected(on_review: Any) -> None:
    with pytest.raises(ValueError, match="on_review must be one of"):
        ACGSMiddleware(on_review=on_review)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_review_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite number of seconds greater than zero"):
        ACGSMiddleware(review_timeout=timeout)


@pytest.mark.parametrize("timeout", ["5", True, None])
def test_non_numeric_review_timeout_is_rejected(timeout: Any) -> None:
    with pytest.raises(ValueError, match="review_timeout must be a number of seconds"):
        ACGSMiddleware(review_timeout=timeout)


async def test_decisions_are_logged(caplog: pytest.LogCaptureFixture, mode: str) -> None:
    caplog.set_level(logging.INFO)
    async with Client(governed(Ran()), mode=mode) as client:
        with pytest.raises(ToolError):
            await client.call_tool("send_email", LEAK)
    assert any("send_email" in record.getMessage() and "ACGS-006" in record.getMessage() for record in caplog.records)
    assert all("hunter2" not in record.getMessage() for record in caplog.records)
