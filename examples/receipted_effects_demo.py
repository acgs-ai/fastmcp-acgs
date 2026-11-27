# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Offline end-to-end ReceiptedEffects pilot demo using FastMCP's in-memory Client."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

# Keep telemetry disabled when running offline without collectors.
os.environ["OTEL_SDK_DISABLED"] = "true"

from acgs_lite import Constitution, Rule, Severity
from acgs_lite.legitimacy import (
    ContractedEffectStore,
    Ed25519ReceiptSigner,
    EffectOutcome,
    EffectRequest,
    EffectStore,
    ProviderContract,
    ProviderContractClass,
    ProviderContractRegistry,
    ProviderResult,
    SignedOutcomeReceipt,
    SignedReceipt,
)
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from fastmcp_acgs import ACGSMiddleware
from fastmcp_acgs.effects import EffectBinding, ReceiptedEffects


class InMemoryRefundProvider:
    """In-memory FENCEABLE refund provider simulating dedup and transient timeouts."""

    contract = ProviderContract(
        provider_id="demo-refunds",
        contract_class=ProviderContractClass.FENCEABLE,
        version="1.0",
        dedup_by_key=True,
        atomic_query_fence=True,
    )

    def __init__(self) -> None:
        self.sends: list[EffectRequest] = []
        self.queries: list[EffectRequest] = []
        self._committed: set[str] = set()
        self.fail_next_send = False

    @property
    def provider_id(self) -> str:
        return self.contract.provider_id

    def send(self, request: EffectRequest) -> ProviderResult:
        self.sends.append(request)
        if self.fail_next_send:
            self.fail_next_send = False
            # Simulate a charge that succeeded remotely just before read timeout.
            self._committed.add(request.key)
            timeout_msg = "simulated gateway timeout after commit"
            raise TimeoutError(timeout_msg)
        self._committed.add(request.key)
        return ProviderResult(request.key, self.provider_id, EffectOutcome.DONE, fenced=False)

    def query_and_fence(self, request: EffectRequest) -> ProviderResult:
        self.queries.append(request)
        outcome = EffectOutcome.DONE if request.key in self._committed else EffectOutcome.NOT_DONE
        return ProviderResult(request.key, self.provider_id, outcome, fenced=outcome is EffectOutcome.NOT_DONE)


def _demo_constitution() -> Constitution:
    return Constitution.from_rules(
        [
            Rule(
                id="DEMO-NO-SECRET",
                text="Block secrets in outbound tool arguments",
                severity=Severity.CRITICAL,
                keywords=["demo-secret"],
            )
        ]
    )


def _build_server(
    db_path: Path,
    provider: InMemoryRefundProvider,
    signer: Ed25519ReceiptSigner,
    emitted: list[SignedOutcomeReceipt],
) -> tuple[FastMCP[Any], ReceiptedEffects]:
    store = EffectStore(db_path)
    store.create_budget("refunds", 10_000)
    registry = ProviderContractRegistry()
    registry.register(provider.contract)
    effects = ReceiptedEffects(
        ContractedEffectStore(store, registry),
        signer,
        budget_id="refunds",
        bindings={
            "issue_refund": EffectBinding(provider, amount=1),
            "record_note": EffectBinding(provider, amount=1),
        },
        on_outcome=emitted.append,
    )
    mcp: FastMCP[Any] = FastMCP(
        "receipted-effects-demo",
        middleware=[ACGSMiddleware(constitution=_demo_constitution(), effects=effects)],
    )

    @mcp.tool
    def issue_refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict[str, Any]:
        """Issue an idempotent refund (body skipped; dispatched via provider)."""
        return {"order_id": order_id, "amount_cents": amount_cents}

    @mcp.tool
    def record_note(body: str, idempotency_key: str) -> dict[str, Any]:
        """Record an operational note (body skipped; dispatched via provider)."""
        return {"body": body}

    return mcp, effects


async def run_demo() -> None:
    signer = Ed25519ReceiptSigner.from_seed(b"fastmcp-acgs-demo-seed-2026-11!!"[:32])
    public_key = signer.public_key_hex()
    provider = InMemoryRefundProvider()
    emitted_before: list[SignedOutcomeReceipt] = []

    with tempfile.TemporaryDirectory(prefix="fastmcp-acgs-demo-") as tmp:
        db_path = Path(tmp) / "effects.sqlite3"
        mcp, _ = _build_server(db_path, provider, signer, emitted_before)

        async with Client(mcp) as client:
            tools = await client.list_tools()
            refund_tool = next(t for t in tools if t.name == "issue_refund")
            has_idem_arg = "idempotency_key" in (refund_tool.inputSchema.get("properties") or {})
            print(f"schema.idempotency_key={has_idem_arg}")

            # 1. ALLOW + signed decision/outcome receipts
            key_ok = "demo-refund-key-0001"
            first = await client.call_tool(
                "issue_refund",
                {"order_id": "ORD-100", "amount_cents": 2500, "idempotency_key": key_ok},
            )
            payload1 = first.structured_content
            if payload1 is None:
                msg = "Expected structured receipt payload from issue_refund call"
                raise RuntimeError(msg)
            dec1 = SignedReceipt.from_dict(payload1["decision_receipt"])
            out1 = SignedOutcomeReceipt.from_dict(payload1["outcome_receipt"])
            print(
                f"allow.outcome={payload1['outcome']} "
                f"decision_verified={dec1.verify(public_key)} "
                f"outcome_verified={out1.verify(public_key)}"
            )

            # 2. Duplicate retry with same idempotency_key returns cached outcome without re-dispatch
            await client.call_tool(
                "issue_refund",
                {"order_id": "ORD-100", "amount_cents": 2500, "idempotency_key": key_ok},
            )
            print(f"retry.provider_sends={len(provider.sends)}")

            # 3. Timeout holds in UNKNOWN, survives restart, and reconciles cleanly
            provider.fail_next_send = True
            key_timeout = "demo-refund-key-0002"
            try:
                await client.call_tool(
                    "issue_refund",
                    {"order_id": "ORD-101", "amount_cents": 1900, "idempotency_key": key_timeout},
                )
            except ToolError as err:
                print(f"timeout.held={'outcome unknown' in str(err).lower()}")
            unknown_key = provider.sends[-1].key

        # Reopen SQLite EffectStore in a fresh server instance and reconcile
        emitted_after: list[SignedOutcomeReceipt] = []
        mcp_restarted, effects_after = _build_server(db_path, provider, signer, emitted_after)
        reconciled = effects_after.reconcile(unknown_key, "issue_refund")
        if reconciled is None:
            msg = "Expected reconciled SignedOutcomeReceipt after timeout"
            raise RuntimeError(msg)
        print(
            f"reconcile.outcome={reconciled.receipt.outcome.value.upper()} "
            f"verified={reconciled.verify(public_key)} emitted={len(emitted_after)}"
        )

        # 4. DENY blocks before provider dispatch
        sends_before_deny = len(provider.sends)
        async with Client(mcp_restarted) as client_restarted:
            try:
                await client_restarted.call_tool(
                    "record_note",
                    {"body": "token=demo-secret", "idempotency_key": "demo-note-key-0003"},
                )
            except ToolError as err:
                zero_dispatch = len(provider.sends) == sends_before_deny
                print(f"deny.blocked={'DEMO-NO-SECRET' in str(err)} zero_dispatch={zero_dispatch}")


if __name__ == "__main__":
    asyncio.run(run_demo())
