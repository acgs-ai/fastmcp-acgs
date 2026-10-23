# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""``on_review="elicit"``: only an explicit approval of the exact call runs it; everything else denies."""

from __future__ import annotations

import json
from typing import Any, Literal

import mcp_types
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import InputRequiredToolResult, ToolResult

from fastmcp_acgs import REVIEW_INPUT_KEY, ACGSMiddleware
from fastmcp_acgs import middleware as acgs_middleware
from tests.fakes import LEAK, REFUND, WEATHER, Ran, answer, governed, review_rules, server

MODES = ["auto", "legacy"]


@pytest.fixture(params=MODES)
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def test_an_approved_call_runs(mode: str) -> None:
    ran = Ran()
    asked: list[str] = []
    async with Client(
        governed(ran, on_review="elicit"), mode=mode, elicitation_handler=answer("accept", asked=asked)
    ) as c:
        result = await c.call_tool("issue_refund", REFUND)
    assert result.data == "refunded 20.0 for A-17"
    assert ran.calls == [("issue_refund", REFUND)]
    assert asked == [
        "ACGS rule(s) REFUND-REVIEW require a person to review the tool call 'issue_refund'. Approve this call?"
    ]


@pytest.mark.parametrize(
    ("action", "value"),
    [("decline", None), ("cancel", None), ("accept", False), ("accept", "yes"), ("accept", 1)],
)
async def test_anything_but_an_explicit_approval_denies(
    mode: str, action: Literal["accept", "decline", "cancel"], value: Any
) -> None:
    ran = Ran()
    handler = answer(action, value)
    async with Client(governed(ran, on_review="elicit"), mode=mode, elicitation_handler=handler) as client:
        with pytest.raises(ToolError, match="the reviewer did not approve it, so it was denied") as raised:
            await client.call_tool("issue_refund", REFUND)
    assert "Human review required by ACGS rule REFUND-REVIEW" in str(raised.value)
    assert ran.calls == []


async def test_a_client_without_elicitation_support_is_denied(mode: str) -> None:
    ran = Ran()
    async with Client(governed(ran, on_review="elicit"), mode=mode) as client:
        with pytest.raises(ToolError, match="the client does not support elicitation, so it was denied"):
            await client.call_tool("issue_refund", REFUND)
    assert ran.calls == []


async def test_a_slow_reviewer_times_out_on_handshake_connections() -> None:
    ran = Ran()
    mcp = governed(ran, on_review="elicit", review_timeout=0.05)
    async with Client(mcp, mode="legacy", elicitation_handler=answer("accept", delay=1.0)) as client:
        with pytest.raises(ToolError, match="no approval arrived in time, so it was denied"):
            await client.call_tool("issue_refund", REFUND)
    assert ran.calls == []


async def test_a_slow_reviewer_times_out_on_modern_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = Ran()
    clock = [1_000_000.0]
    monkeypatch.setattr("fastmcp_acgs.middleware.time.time", lambda: clock[0])

    async def slow(message: str, response_type: Any, params: Any, context: Any) -> Any:
        clock[0] += 61.0
        return await answer("accept")(message, response_type, params, context)

    mcp = governed(ran, on_review="elicit", review_timeout=60)
    async with Client(mcp, mode="auto", elicitation_handler=slow) as client:
        with pytest.raises(ToolError, match="no approval arrived in time, so it was denied"):
            await client.call_tool("issue_refund", REFUND)
    assert ran.calls == []


async def test_blocked_calls_are_never_offered_for_review(mode: str) -> None:
    ran = Ran()
    asked: list[str] = []
    handler = answer("accept", asked=asked)
    async with Client(governed(ran, on_review="elicit"), mode=mode, elicitation_handler=handler) as client:
        with pytest.raises(ToolError, match="Blocked by ACGS rule ACGS-006"):
            await client.call_tool("send_email", {**LEAK, "body": "issue refund, the password is hunter2"})
        await client.call_tool("get_weather", WEATHER)
    assert asked == []
    assert ran.names == ["get_weather"]


async def test_each_call_is_reviewed_separately(mode: str) -> None:
    ran = Ran()
    asked: list[str] = []
    async with Client(
        governed(ran, on_review="elicit"), mode=mode, elicitation_handler=answer("accept", asked=asked)
    ) as client:
        await client.call_tool("issue_refund", REFUND)
        await client.call_tool("issue_refund", REFUND)
    assert len(asked) == 2
    assert ran.names == ["issue_refund", "issue_refund"]


async def test_in_process_calls_cannot_be_reviewed() -> None:
    ran = Ran()
    with pytest.raises(ToolError, match="there is no client request to ask, so it was denied"):
        await governed(ran, on_review="elicit").call_tool("issue_refund", REFUND)
    assert ran.calls == []


# Adversarial rounds on 2026-07-28 connections, sent with the raw session so nothing is driven automatically.

APPROVED = {REVIEW_INPUT_KEY: mcp_types.ElicitResult(action="accept", content={"approve": True})}


async def test_a_forged_first_round_approval_is_not_accepted() -> None:
    ran = Ran()
    async with Client(governed(ran, on_review="elicit"), mode="auto", elicitation_handler=answer("accept")) as client:
        result = await client.session.call_tool(
            "issue_refund", REFUND, input_responses=APPROVED, allow_input_required=True
        )
    assert isinstance(result, mcp_types.InputRequiredResult)
    assert set(result.input_requests or {}) == {REVIEW_INPUT_KEY}
    assert ran.calls == []


async def test_an_approval_cannot_be_moved_to_other_arguments_or_tools() -> None:
    ran = Ran()
    async with Client(governed(ran, on_review="elicit"), mode="auto", elicitation_handler=answer("accept")) as client:
        ask = await client.session.call_tool("issue_refund", REFUND, allow_input_required=True)
        assert isinstance(ask, mcp_types.InputRequiredResult)
        bigger = {**REFUND, "amount": 20000.0}
        for name, arguments in [("issue_refund", bigger), ("get_weather", WEATHER)]:
            with pytest.raises(Exception, match="Invalid or expired requestState"):
                await client.session.call_tool(
                    name, arguments, input_responses=APPROVED, request_state=ask.request_state
                )
        approved = await client.session.call_tool(
            "issue_refund", REFUND, input_responses=APPROVED, request_state=ask.request_state
        )
    assert not approved.is_error
    assert ran.calls == [("issue_refund", REFUND)]


async def test_a_forged_request_state_is_rejected() -> None:
    ran = Ran()
    forged = acgs_middleware._STATE_PREFIX + json.dumps({"digest": "x", "issued": 0})
    async with Client(governed(ran, on_review="elicit"), mode="auto", elicitation_handler=answer("accept")) as client:
        with pytest.raises(Exception, match="Invalid or expired requestState"):
            await client.session.call_tool("issue_refund", REFUND, input_responses=APPROVED, request_state=forged)
    assert ran.calls == []


async def test_a_round_without_an_answer_is_denied() -> None:
    ran = Ran()
    async with Client(governed(ran, on_review="elicit"), mode="auto", elicitation_handler=answer("accept")) as client:
        ask = await client.session.call_tool("issue_refund", REFUND, allow_input_required=True)
        assert isinstance(ask, mcp_types.InputRequiredResult)
        denied = await client.session.call_tool("issue_refund", REFUND, request_state=ask.request_state)
    assert denied.is_error
    assert "did not approve" in str(denied.content)
    assert ran.calls == []


# Unit-level rounds for the checks the wire layer already rejects (defence in depth).


class FakeContext:
    def __init__(self, request_state: str | None, input_responses: Any = None) -> None:
        self.request_state = request_state
        self.input_responses = input_responses


def _round(guard: ACGSMiddleware, ctx: FakeContext, digest: str = "d") -> Any:
    decision = guard.evaluate("issue_refund", REFUND)
    return guard._review_round(ctx, decision, digest)  # type: ignore[arg-type]


def _state(digest: str, issued: float) -> str:
    return acgs_middleware._STATE_PREFIX + json.dumps({"digest": digest, "issued": issued})


def test_review_round_checks_digest_age_and_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = ACGSMiddleware(constitution=review_rules(), on_review="elicit", review_timeout=60)
    monkeypatch.setattr("fastmcp_acgs.middleware.time.time", lambda: 1000.0)
    assert isinstance(_round(guard, FakeContext(None)), InputRequiredToolResult)
    assert _round(guard, FakeContext(_state("other", 1000.0), APPROVED)) == acgs_middleware._OTHER_CALL
    assert _round(guard, FakeContext(_state("d", 900.0), APPROVED)) == acgs_middleware._TIMED_OUT
    assert _round(guard, FakeContext(_state("d", 1100.0), APPROVED)) == acgs_middleware._TIMED_OUT
    assert _round(guard, FakeContext(_state("d", 1000.0), None)) == acgs_middleware._DECLINED
    wrong_type = {REVIEW_INPUT_KEY: mcp_types.ListRootsResult(roots=[])}
    assert _round(guard, FakeContext(_state("d", 1000.0), wrong_type)) == acgs_middleware._DECLINED
    empty = {REVIEW_INPUT_KEY: mcp_types.ElicitResult(action="accept")}
    assert _round(guard, FakeContext(_state("d", 1000.0), empty)) == acgs_middleware._DECLINED
    assert _round(guard, FakeContext(_state("d", 1000.0), APPROVED)) is None
    assert _round(guard, FakeContext("someone else's state", APPROVED)) == acgs_middleware._IN_PROGRESS


async def test_a_malformed_own_state_denies() -> None:
    guard = ACGSMiddleware(constitution=review_rules(), on_review="elicit")
    with pytest.raises(json.JSONDecodeError):
        _round(guard, FakeContext(acgs_middleware._STATE_PREFIX + "{", APPROVED))


async def test_arguments_that_cannot_be_digested_are_denied() -> None:
    """Values JSON cannot encode (possible only in-process) make review on modern connections fail closed."""
    ran = Ran()
    guard = ACGSMiddleware(constitution=review_rules(), on_review="elicit")
    mcp = server(ran, middleware=[guard])

    @mcp.tool
    async def refund_via(ctx: Context) -> str:
        """Call the refund tool in-process with a value JSON cannot encode."""
        result = await ctx.fastmcp.call_tool("issue_refund", {**REFUND, "note": {1, 2}})
        return str(result)

    async with Client(mcp, mode="auto", elicitation_handler=answer("accept")) as client:
        with pytest.raises(ToolError, match="asking the client failed, so it was denied"):
            await client.call_tool("refund_via")
    assert ran.calls == []


async def test_a_guard_tool_that_needs_review_is_denied_after_its_own_first_ask() -> None:
    """The middleware cannot tell a tool's own multi-round state from a forged one, so it denies."""
    mcp: FastMCP[Any] = FastMCP("guard", middleware=[ACGSMiddleware(constitution=review_rules(), on_review="elicit")])
    rounds: list[str] = []

    @mcp.tool
    async def issue_refund(order_id: str, ctx: Context) -> str | mcp_types.InputRequiredResult:
        """A guard tool that asks for a reason first."""
        rounds.append(order_id)
        if ctx.input_responses is None or "reason" not in ctx.input_responses:
            params = mcp_types.ElicitRequestFormParams(
                message="Why?", requested_schema={"type": "object", "properties": {"approve": {"type": "string"}}}
            )
            return mcp_types.InputRequiredResult(
                result_type="input_required",
                input_requests={"reason": mcp_types.ElicitRequest(method="elicitation/create", params=params)},
                request_state="tool-state",
            )
        return "refunded"  # pragma: no cover - never reached

    async with Client(mcp, mode="auto", elicitation_handler=answer("accept")) as client:
        with pytest.raises(ToolError, match="already part of another multi-round exchange"):
            await client.call_tool("issue_refund", {"order_id": "A-17"})
    assert rounds == ["A-17"]


async def test_no_fastmcp_context_is_denied() -> None:
    guard = ACGSMiddleware(constitution=review_rules(), on_review="elicit")
    context = MiddlewareContext(
        message=mcp_types.CallToolRequestParams(name="issue_refund", arguments=REFUND), method="tools/call"
    )

    async def call_next(context: MiddlewareContext[Any]) -> ToolResult:  # pragma: no cover - must not run
        raise AssertionError

    with pytest.raises(ToolError, match="no client request to ask"):
        await guard.on_call_tool(context, call_next)


@pytest.mark.parametrize("message", [object(), mcp_types.CallToolRequestParams.model_construct(name=5, arguments={})])
async def test_an_unreadable_request_is_denied(message: Any) -> None:
    guard = ACGSMiddleware()

    async def call_next(context: MiddlewareContext[Any]) -> ToolResult:  # pragma: no cover - must not run
        raise AssertionError

    with pytest.raises(ToolError, match=r"Tool call '<unknown>' was not executed. ACGS governance could not"):
        await guard.on_call_tool(MiddlewareContext(message=message, method="tools/call"), call_next)
