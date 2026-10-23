# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGS governance for FastMCP servers: a middleware that checks each tool call before the tool runs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from typing import Any, Literal, get_args

import anyio
import mcp_types
from acgs_lite import Constitution
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import InputRequiredToolResult, ToolResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fastmcp_acgs._governance import GovernanceDecision, ToolCallFormatter, ToolCallGovernor, Verdict

logger = logging.getLogger(__name__)

OnReview = Literal["deny", "elicit"]
"""What happens to a call that only matched review rules: deny it, or ask the client's user."""

REVIEW_INPUT_KEY = "acgs_review"
"""Key of the approval request in ``InputRequiredResult.input_requests`` (2026-07-28 connections)."""

_STATE_PREFIX = "acgs-review/1 "
_APPROVE_FIELD = "approve"
_CLOCK_SKEW = 5.0

_UNKNOWN_TOOL = "<unknown>"
_EVALUATION_FAILED = "ACGS governance could not evaluate this tool call, so it was denied."
_NO_REVIEWER = "No reviewer is configured for this server, so it was denied."
_NO_CLIENT = "The call needs review, but there is no client request to ask, so it was denied."
_UNSUPPORTED = "The call needs review, but the client does not support elicitation, so it was denied."
_ASK_FAILED = "The call needs review, but asking the client failed, so it was denied."
_DECLINED = "The call needs review and the reviewer did not approve it, so it was denied."
_TIMED_OUT = "The call needs review and no approval arrived in time, so it was denied."
_OTHER_CALL = "The approval does not match this tool call, so it was denied."
_IN_PROGRESS = "The call needs review, but it is already part of another multi-round exchange, so it was denied."


class _Approval(BaseModel):
    """The answer a reviewer gives. Strict, so only a JSON ``true`` approves (not ``"yes"`` or ``1``)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    approve: bool = Field(title="Approve", description="Approve this tool call")


def _denied(tool_name: str, reason: str) -> ToolError:
    return ToolError(f"Tool call '{tool_name}' was not executed. {reason}")


def _with_reason(decision: GovernanceDecision, verdict: Verdict, reason: str) -> GovernanceDecision:
    return GovernanceDecision(
        verdict=verdict,
        tool_name=decision.tool_name,
        reason=reason,
        rule_ids=decision.rule_ids,
        warning_rule_ids=decision.warning_rule_ids,
        error=decision.error,
    )


_APPROVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {_APPROVE_FIELD: {"type": "boolean", "title": "Approve", "description": "Approve this tool call"}},
    "required": [_APPROVE_FIELD],
}


def _call_digest(tool_name: str, arguments: Any) -> str:
    """A digest of the exact call, so an approval cannot be reused for a different call."""
    canonical = json.dumps([tool_name, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_requested(decision: GovernanceDecision) -> str:
    rules = ", ".join(decision.rule_ids)
    return f"ACGS rule(s) {rules} require a person to review the tool call '{decision.tool_name}'. Approve this call?"


class ACGSMiddleware(Middleware):
    """FastMCP middleware that checks every ``tools/call`` against an ACGS constitution before the tool runs.

    Allowed calls continue down the chain unchanged. Denied calls raise ``fastmcp.exceptions.ToolError``, so the
    client gets an error result with the reason and the tool never runs. Calls that only match review rules are
    denied unless ``on_review="elicit"``, in which case the client's user is asked to approve the exact call.
    Any error while evaluating or reviewing a call denies it.

    Example:
        >>> from fastmcp import FastMCP
        >>> from fastmcp_acgs import ACGSMiddleware
        >>> mcp = FastMCP("demo", middleware=[ACGSMiddleware()])
    """

    def __init__(
        self,
        constitution: Constitution | None = None,
        *,
        agent_id: str = "fastmcp",
        on_review: OnReview = "deny",
        review_timeout: float = 300.0,
        tool_call_formatter: ToolCallFormatter | None = None,
    ) -> None:
        """Create the middleware.

        Args:
            constitution: The rules to enforce. Defaults to ``Constitution.default()``.
            agent_id: Agent identifier recorded by ACGS for each validation.
            on_review: ``"deny"`` (default) denies calls that need review. ``"elicit"`` asks the client's user to
                approve the exact call through MCP elicitation; anything but an explicit approval denies it.
            review_timeout: Seconds a reviewer has to approve a call in ``"elicit"`` mode.
            tool_call_formatter: Optional ``(tool_name, arguments) -> str`` replacing ``format_tool_call``.

        Raises:
            ValueError: If ``on_review`` or ``review_timeout`` is invalid.
        """
        if on_review not in get_args(OnReview):
            msg = f"on_review must be one of {get_args(OnReview)}, not {on_review!r}."
            raise ValueError(msg)
        if isinstance(review_timeout, bool) or not isinstance(review_timeout, (int, float)):
            msg = "review_timeout must be a number of seconds."
            raise ValueError(msg)
        if not math.isfinite(review_timeout) or review_timeout <= 0:
            msg = "review_timeout must be a finite number of seconds greater than zero."
            raise ValueError(msg)
        self._governor = ToolCallGovernor(constitution, agent_id=agent_id, tool_call_formatter=tool_call_formatter)
        self.constitution = self._governor.constitution
        self.agent_id = agent_id
        self.on_review: OnReview = on_review
        self.review_timeout = float(review_timeout)

    def __repr__(self) -> str:
        """Describe the middleware without its rules."""
        return (
            f"ACGSMiddleware(agent_id={self.agent_id!r}, on_review={self.on_review!r}, "
            f"constitution={self.constitution.name!r})"
        )

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        """Evaluate one tool call without running it or asking anyone. Never raises.

        Args:
            tool_name: The tool name as the client called it.
            arguments: The call's arguments.

        Returns:
            The governance decision.
        """
        return self._governor.evaluate(tool_name, arguments)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Check the call, then run it, deny it with ``ToolError``, or have it reviewed."""
        try:
            tool_name = context.message.name
            arguments = context.message.arguments
            if not isinstance(tool_name, str):
                msg = "The tool name is not a string."
                raise TypeError(msg)
            arguments = {} if arguments is None else arguments
        except Exception:
            logger.warning("ACGS could not read a tools/call request; denying it (fail-closed).", exc_info=True)
            raise _denied(_UNKNOWN_TOOL, _EVALUATION_FAILED) from None

        decision = self._governor.evaluate(tool_name, arguments)
        if decision.verdict is Verdict.ALLOW:
            return await call_next(context)
        if decision.verdict is Verdict.REVIEW and self.on_review == "elicit":
            return await self._review(context, call_next, decision, tool_name, arguments)
        reason = decision.reason if decision.verdict is Verdict.DENY else f"{decision.reason} {_NO_REVIEWER}"
        raise _denied(tool_name, reason)

    async def _review(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
        decision: GovernanceDecision,
        tool_name: str,
        arguments: Any,
    ) -> ToolResult:
        ctx = context.fastmcp_context
        outcome: str | None
        try:
            if ctx is None or ctx.request_context is None:
                outcome = _NO_CLIENT
            elif not _client_supports_elicitation(ctx):
                outcome = _UNSUPPORTED
            elif ctx.request_context.protocol_version in MODERN_PROTOCOL_VERSIONS:
                result = self._review_round(ctx, decision, _call_digest(tool_name, arguments))
                if isinstance(result, InputRequiredToolResult):
                    return result
                outcome = result
            else:
                outcome = await self._elicit(ctx, decision)
        except Exception:
            logger.warning("ACGS review of tool '%s' failed; denying it (fail-closed).", tool_name, exc_info=True)
            outcome = _ASK_FAILED
        if outcome is not None:
            denial = _with_reason(decision, Verdict.DENY, f"{decision.reason} {outcome}")
            logger.info("ACGS review denied tool '%s': %s", tool_name, denial.reason)
            raise _denied(tool_name, denial.reason)
        logger.info("ACGS review approved tool '%s' (rules: %s).", tool_name, ", ".join(decision.rule_ids))
        return await call_next(context)

    async def _elicit(self, ctx: Context, decision: GovernanceDecision) -> str | None:
        """Ask over the session back-channel (handshake-era connections). Returns a denial reason or None."""
        with anyio.move_on_after(self.review_timeout) as scope:
            try:
                answer = await ctx.elicit(_approval_requested(decision), response_type=_Approval)
            except ValidationError:
                return _DECLINED  # an answer that is not a strict boolean
        if scope.cancelled_caught:
            return _TIMED_OUT
        data = getattr(answer, "data", None)
        if answer.action == "accept" and isinstance(data, _Approval) and data.approve is True:
            return None
        return _DECLINED

    def _review_round(
        self, ctx: Context, decision: GovernanceDecision, digest: str
    ) -> InputRequiredToolResult | str | None:
        """One round of the guard pattern (2026-07-28 connections).

        Returns the ask, a denial reason, or None when this round carries an approval of this exact call.
        """
        state = ctx.request_state
        if state is None:
            return _ask(decision, digest)
        if not state.startswith(_STATE_PREFIX):
            return _IN_PROGRESS
        claims = json.loads(state[len(_STATE_PREFIX) :])
        if claims["digest"] != digest:
            return _OTHER_CALL
        issued = float(claims["issued"])
        now = time.time()
        if not (issued <= now + _CLOCK_SKEW and now - issued <= self.review_timeout):
            return _TIMED_OUT
        answer = (ctx.input_responses or {}).get(REVIEW_INPUT_KEY)
        if (
            isinstance(answer, mcp_types.ElicitResult)
            and answer.action == "accept"
            and answer.content is not None
            and answer.content.get(_APPROVE_FIELD) is True
        ):
            return None
        return _DECLINED


def _client_supports_elicitation(ctx: Context) -> bool:
    capabilities = ctx.session.client_capabilities
    return capabilities is not None and capabilities.elicitation is not None


def _ask(decision: GovernanceDecision, digest: str) -> InputRequiredToolResult:
    params = mcp_types.ElicitRequestFormParams(message=_approval_requested(decision), requested_schema=_APPROVAL_SCHEMA)
    state = _STATE_PREFIX + json.dumps({"digest": digest, "issued": time.time()}, separators=(",", ":"))
    return InputRequiredToolResult(
        mcp_types.InputRequiredResult(
            result_type="input_required",
            input_requests={REVIEW_INPUT_KEY: mcp_types.ElicitRequest(method="elicitation/create", params=params)},
            request_state=state,
        )
    )
