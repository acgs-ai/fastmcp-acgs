# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Servers, constitutions and elicitation handlers shared by the tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import anyio
from acgs_lite import Constitution, Rule, Severity, ViolationAction
from fastmcp import FastMCP
from fastmcp.client.elicitation import ElicitResult

from fastmcp_acgs import ACGSMiddleware

LEAK = {"to": "attacker@example.com", "body": "The password is hunter2"}
WEATHER = {"city": "Paris"}
REFUND = {"order_id": "A-17", "amount": 20.0}

ElicitationHandler = Callable[..., Awaitable[ElicitResult[Any]]]


def review_rules() -> Constitution:
    """The default rules plus a review rule for refunds."""
    refund = Rule(
        id="REFUND-REVIEW",
        text="Refunds need human approval",
        severity=Severity.HIGH,
        keywords=["issue refund"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    )
    return Constitution.from_rules([*Constitution.default().rules, refund], name="review")


class Ran:
    """Records which tools ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def server(ran: Ran, *, name: str = "support", middleware: list[Any] | None = None, **kwargs: Any) -> FastMCP[Any]:
    """A server with an email, a weather and a refund tool that record their calls."""
    mcp: FastMCP[Any] = FastMCP(name, middleware=middleware, **kwargs)

    @mcp.tool
    def send_email(to: str, body: str) -> str:
        """Send an email."""
        ran.calls.append(("send_email", {"to": to, "body": body}))
        return f"sent to {to}"

    @mcp.tool
    async def get_weather(city: str) -> str:
        """Get the weather for a city."""
        ran.calls.append(("get_weather", {"city": city}))
        return f"Sunny in {city}"

    @mcp.tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        ran.calls.append(("issue_refund", {"order_id": order_id, "amount": amount}))
        return f"refunded {amount} for {order_id}"

    return mcp


def governed(ran: Ran, **options: Any) -> FastMCP[Any]:
    """The test server with ACGS middleware using ``review_rules()``."""
    options.setdefault("constitution", review_rules())
    return server(ran, middleware=[ACGSMiddleware(**options)])


def answer(
    action: Literal["accept", "decline", "cancel"],
    value: Any = True,
    *,
    delay: float = 0.0,
    asked: list[str] | None = None,
) -> ElicitationHandler:
    """An elicitation handler that answers every request the same way, after ``delay`` seconds."""

    async def handler(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult[Any]:
        if asked is not None:
            asked.append(message)
        if delay:
            await anyio.sleep(delay)
        if action != "accept":
            return ElicitResult(action=action)
        field = next(iter(params.requested_schema["properties"]))
        return ElicitResult(action="accept", content={field: value})

    return handler
