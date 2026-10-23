# fastmcp-acgs

[![CI](https://github.com/acgs-ai/fastmcp-acgs/actions/workflows/ci.yml/badge.svg)](https://github.com/acgs-ai/fastmcp-acgs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/fastmcp-acgs.svg)](https://pypi.org/project/fastmcp-acgs/)
[![Python](https://img.shields.io/pypi/pyversions/fastmcp-acgs.svg)](https://pypi.org/project/fastmcp-acgs/)

`fastmcp-acgs` checks the tool calls a [FastMCP](https://github.com/PrefectHQ/fastmcp) server receives against
[ACGS](https://pypi.org/project/acgs-lite/) constitution rules before the tool runs. It is one FastMCP
[middleware](https://gofastmcp.com/servers/middleware), `ACGSMiddleware`, that implements `on_call_tool`.

What happens to a call:

- Calls that match no blocking rule run unchanged.
- Calls that match a blocking rule do not run. The middleware raises `fastmcp.exceptions.ToolError`, so the client
  gets an error result with the reason.
- Calls that only match review rules are denied by default. With `on_review="elicit"`, the client's user is asked
  to approve the exact call through MCP elicitation; the call runs only on an explicit approval.
- A blocking rule always wins. An approval never runs a blocked call.
- Any error while evaluating or reviewing a call denies it (fail-closed).

The package is a thin adapter over the public `acgs-lite` API.

> [!NOTE]
> This is a community package. It is not part of FastMCP and is not maintained, reviewed or endorsed by the
> FastMCP maintainers.

## Installation

```bash
pip install fastmcp-acgs
```

Requires Python 3.10 or later, `fastmcp-slim[server]>=4.1.0,<5` and `acgs-lite>=3.0.1,<4`. It works with the
`fastmcp` package as well, which installs `fastmcp-slim[server,client]`. FastMCP 2.x and 3.x are not supported.

## Usage

```python
from fastmcp import FastMCP

from fastmcp_acgs import ACGSMiddleware

mcp = FastMCP("support", middleware=[ACGSMiddleware()])  # or mcp.add_middleware(ACGSMiddleware()), added last


@mcp.tool
def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"
```

## Quickstart

`examples/quickstart.py` runs offline with an in-memory client:

```python
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
```

```text
send_email: Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).
get_weather: Sunny in Paris
asked: ACGS rule(s) REFUND-REVIEW require a person to review the tool call 'issue_refund'. Approve this call?
issue_refund: refunded 20.0 for A-17
```

## How a call is evaluated

1. The tool name and the call's arguments are rendered as text by `format_tool_call`:

   ```python
   from fastmcp_acgs import format_tool_call

   print(format_tool_call("update_config", {"service": "billing", "change": "disable\nlogging", "dryRun": False}))
   ```

   ```text
   tool: update_config
   arguments:
   change: disable logging
   dryRun: false
   service: billing
   normalized: update config | dry run
   ```

   Each argument leaf is one `path: value` line (nested values use `a.b[0].c` paths), so both argument names and
   values are visible to rules. Whitespace inside values is collapsed so phrases split across lines still match.
   Identifiers written in `snake_case`, `kebab-case` or `camelCase` (the tool name, argument names, and
   single-token string values) are repeated as plain words on the `normalized:` line, so the keyword
   `skip audit` also matches a tool called `skip_audit`. Nothing is truncated.
   Pass `tool_call_formatter=` to use a different representation.

   The name is the one the client called (for a mounted server, the namespaced name such as `child_send_email`),
   and the arguments are the ones in the request, before FastMCP validates them.

2. The text is validated with `acgs_lite.GovernanceEngine.validate` against the constitution
   (`Constitution.default()` unless you pass one).

3. The result decides what happens to the call:

   | ACGS outcome | Result |
   |---|---|
   | No blocking rule matched (warn-only rules may match) | The tool runs |
   | Only `require_human_review` / `escalate_to_senior` rules matched | Review (see below) |
   | Any `block`, `block_and_notify` or `halt_and_alert` rule matched | Denied |
   | A rule ID not found in the constitution, or an invalid result without a rule | Denied |
   | Any exception while reading or evaluating the call | Denied (fail-closed) |

   A matched blocking rule is never downgraded to review because a review rule matched as well.

A denied call is not executed. The middleware raises `ToolError("Tool call '<name>' was not executed. <reason>")`.
FastMCP sends `ToolError` messages to the client even with `mask_error_details=True`. The reason names the
matched rules but never repeats argument values. `ACGSMiddleware.evaluate(tool_name, arguments)` returns the
`GovernanceDecision` without running anything.

## Review

- `on_review="deny"` (default): calls that need review are denied with "No reviewer is configured for this
  server, so it was denied."
- `on_review="elicit"`: the client is asked "ACGS rule(s) ... require a person to review the tool call '<name>'.
  Approve this call?" with a form that has one boolean field, `approve`. Only `accept` with `approve: true`
  (a JSON boolean, not `"yes"` or `1`) runs the call. Everything else denies it:

  | Situation | Result |
  |---|---|
  | The user declines or cancels, or answers `approve: false` | Denied |
  | The client did not declare the elicitation capability | Denied |
  | No answer within `review_timeout` seconds (default 300) | Denied |
  | Asking fails (for example the client errors, or the call came from in-process code with no client) | Denied |

```python
from fastmcp import FastMCP

from fastmcp_acgs import ACGSMiddleware

mcp = FastMCP("payments", middleware=[ACGSMiddleware(constitution=constitution, on_review="elicit")])
```

FastMCP 4 supports both MCP protocol eras, and the middleware uses the matching mechanism:

- **2025-11-25 and earlier (handshake):** the middleware calls `ctx.elicit()` and waits for the answer.
- **2026-07-28 (modern):** there is no server-to-client request, so the middleware returns an
  `InputRequiredResult` (key `acgs_review`) instead of running the tool. The client asks its user and sends the
  call again with the answer. The middleware puts a SHA-256 digest of the tool name and arguments, and the time
  it asked, into `requestState`; FastMCP seals that state and binds it to the call. The call runs only if the next
  round carries that state, the same name and arguments, an answer under `acgs_review` that approves, and arrives
  within `review_timeout`. `fastmcp.Client` with an `elicitation_handler` drives this automatically.

Things to know:

- The client is the reviewer. The middleware cannot tell whether a person or a program answered. Use
  `"elicit"` only with clients you trust to show the question to a person; otherwise keep `"deny"`.
- On modern connections the approval travels with the request, so a client can repeat the same approved round
  (same tool, same arguments) until `review_timeout` or FastMCP's state expiry passes. It cannot reuse it for
  other arguments or another tool.
- The question names the tool and the rules, not the arguments. Clients usually show the pending tool call.

## What is covered

| Path | Checked | Notes |
|---|---|---|
| `tools/call` from any client (stdio, HTTP, in-memory) | Yes | |
| Tools of mounted servers, called through the parent | Yes | Under the namespaced name |
| Tools of a proxy server (`create_proxy`) | Yes | Add the middleware to the proxy or to the backend |
| `server.call_tool(...)` in-process, and tools that call other tools through `ctx.fastmcp.call_tool` | Yes | Review needs a client request, so in-process review is denied |
| `call_tool` of the search transforms (tested with `RegexSearchTransform`) | Yes | Both the wrapper call and the inner call are checked |
| Renamed tools (`ToolTransform`) | Yes | Under the name the client sees |
| `server.call_tool(..., run_middleware=False)` | No | FastMCP skips all middleware |
| The child server of a mount, or the backend of a proxy, reached directly | No | Add the middleware there as well |
| Resources, resource templates and prompts | No | Only tool calls are checked |

## Middleware order

FastMCP runs middleware in the order added, the first one outermost. Add `ACGSMiddleware` last, so it sees the
arguments the tool will get. A middleware added after it can change the call after the check; one added before
it is checked.

## Custom constitutions

```python
from acgs_lite import Constitution, Rule, Severity, ViolationAction
from fastmcp import FastMCP

from fastmcp_acgs import ACGSMiddleware

constitution = Constitution.from_rules(
    [
        Rule(
            id="NO-DESTRUCTIVE-SQL",
            text="No destructive SQL",
            severity=Severity.CRITICAL,
            keywords=["drop table", "truncate table"],
        ),
        Rule(
            id="REFUND-REVIEW",
            text="Refunds need approval",
            severity=Severity.HIGH,
            keywords=["refund"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ],
    name="payments",
)
mcp = FastMCP("payments", middleware=[ACGSMiddleware(constitution=constitution, agent_id="payments-server")])
```

`Constitution.from_yaml("rules.yaml")` works as well.

## Options

| Argument | Default | Meaning |
|---|---|---|
| `constitution` | `Constitution.default()` | Rules to enforce |
| `agent_id` | `"fastmcp"` | Agent identifier recorded by ACGS |
| `on_review` | `"deny"` | `"deny"`, or `"elicit"` (the client's user approves the exact call) |
| `review_timeout` | `300` | Seconds a reviewer has to approve a call in `"elicit"` mode |
| `tool_call_formatter` | `format_tool_call` | `(tool_name, arguments) -> str` used as the evaluated text |

## Limitations

- Only the paths listed as checked in [What is covered](#what-is-covered). Tool results, resources and prompts
  are not checked.
- Values a tool transform adds after the middleware ran (for example a hidden argument with a default) are not
  part of the evaluated text.
- On 2026-07-28 connections, `on_review="elicit"` does not work on a proxy server: FastMCP forwards the approval
  round to the backend, which rejects the review state as "Invalid or expired requestState". The call does not
  run. Put the middleware on the backend instead, or use `"deny"` on the proxy.
- On 2026-07-28 connections, a tool that itself asks for input (a guard tool returning `InputRequiredResult`)
  and needs review is denied once it asks, because its state cannot be told apart from a forged one. A nested
  call that needs review inside a call that was itself reviewed is denied as well, since one round carries one
  approval. Both work on handshake connections.
- Background tasks (`task=True`) are not tested.
- Tested with the in-memory client and a stdio subprocess, on both protocol eras, with mounted and proxy servers,
  the search transform and tool transforms, against fastmcp-slim 4.1.0 and the latest 4.x release. HTTP
  transports, authentication providers and real MCP hosts are not tested.
- `Constitution.default()` focuses on governance integrity (self-approval, skipping audits, privilege escalation)
  and on secrets and personal data (`password`, `api_key`, `sk-...` and `ghp_...` tokens, US SSN
  patterns). It does **not** cover destructive operations such as `rm -rf` or `DROP TABLE`. Write rules for the
  tools you expose. `tests/test_default_constitution.py` lists what the default rules do and do not catch.
- Keyword rules in acgs-lite 3.0.1 match literal, case-insensitive substrings. They do not understand negation
  ("never share the password" matches `password`) and do not treat `-`, `_` and spaces as equivalent
  (`api-key` does not match the keyword `api_key`).
- Because argument names are part of the evaluated text, a tool with a parameter named `password` is denied
  under the default constitution whenever it is called.

## Development

```bash
uv sync --group dev        # or use hatch: hatch run prepare
hatch run format           # ruff format + ruff check --fix
hatch run lint             # ruff check + ruff format --check
hatch run typecheck        # mypy --strict
hatch run cov              # pytest with a 100% branch-coverage gate, network disabled
hatch run min:cov          # the same tests against fastmcp-slim 4.1.0 and acgs-lite 3.0.1
pre-commit install         # format, lint, type check and tests on commit; commitizen on commit messages
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); `cz bump` updates the version
and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE).
