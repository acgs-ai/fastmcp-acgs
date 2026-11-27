# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Receipted effects, end to end through the in-memory FastMCP client on both protocol eras."""

from __future__ import annotations

import dataclasses
import enum
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import anyio
import mcp_types
import pytest
from acgs_lite import legitimacy
from acgs_lite.legitimacy import (
    BASELINE_CONSTRAINT_MARKER,
    RECEIPT_SCHEMA_V2,
    ContractedEffectStore,
    Ed25519ReceiptSigner,
    EffectOutcome,
    EffectRequest,
    EffectState,
    EffectStore,
    ProviderContract,
    ProviderContractClass,
    ProviderContractRegistry,
    ProviderResult,
    SignedOutcomeReceipt,
    SignedReceipt,
    bind_policy,
    receipt_identity,
    signed_envelope_identity,
)
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import Tool, ToolResult
from pydantic import AfterValidator, AliasChoices, BaseModel, Field, field_serializer
from pydantic_core import to_jsonable_python

from fastmcp_acgs import ACGSMiddleware, GovernanceDecision, Verdict
from fastmcp_acgs import effects as effects_module
from fastmcp_acgs.effects import (
    IDEMPOTENCY_ARGUMENT,
    IDEMPOTENCY_KEY_META,
    EffectBinding,
    ReceiptedEffects,
    grant_identity,
)
from fastmcp_acgs.middleware import _call_digest
from tests.fakes import LEAK, REFUND, Ran, answer, review_rules

MODES = ["auto", "legacy"]
PROVIDER = "payments"
BUDGET = "refunds"
SEED = b"\x07" * 32
ORDER = {"order_id": "A-17"}


@pytest.fixture(params=MODES)
def mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class FakeProvider:
    """A provider that counts sends and queries. It deduplicates by key, like a FENCEABLE provider must."""

    def __init__(
        self,
        provider_id: str = PROVIDER,
        *,
        outcome: EffectOutcome = EffectOutcome.DONE,
        fail: bool = False,
        query_outcome: EffectOutcome = EffectOutcome.DONE,
    ) -> None:
        self._provider_id = provider_id
        self.outcome = outcome
        self.fail = fail
        self.query_outcome = query_outcome
        self.sends: list[EffectRequest] = []
        self.queries: list[EffectRequest] = []

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def send(self, request: EffectRequest) -> ProviderResult:
        self.sends.append(request)
        if self.fail:
            msg = "provider timed out"
            raise TimeoutError(msg)
        fenced = self.outcome is not EffectOutcome.DONE
        return ProviderResult(request.key, self._provider_id, self.outcome, fenced=fenced)

    def query_and_fence(self, request: EffectRequest) -> ProviderResult:
        self.queries.append(request)
        return ProviderResult(request.key, self._provider_id, self.query_outcome, fenced=True)


def contract(kind: ProviderContractClass = ProviderContractClass.FENCEABLE) -> ProviderContract:
    if kind is ProviderContractClass.FENCEABLE:
        return ProviderContract(PROVIDER, kind, "1", dedup_by_key=True, atomic_query_fence=True)
    if kind is ProviderContractClass.BOUNDED:
        return ProviderContract(PROVIDER, kind, "1", False, False, delivery_bound_seconds=30)
    return ProviderContract(PROVIDER, kind, "1", dedup_by_key=False, atomic_query_fence=False)


class Harness:
    """A store, a signer, the receipts emitted, and a governed server with bound and unbound tools."""

    def __init__(
        self,
        path: Path,
        provider: FakeProvider,
        *,
        kind: ProviderContractClass | None = ProviderContractClass.FENCEABLE,
        amount: int = 1,
        **options: Any,
    ) -> None:
        self.path = path
        self.provider = provider
        self.signer = Ed25519ReceiptSigner.from_seed(SEED)
        self.outcomes: list[SignedOutcomeReceipt] = []
        store = EffectStore(path)
        store.create_budget(BUDGET, 100)
        registry = ProviderContractRegistry()
        if kind is not None:
            registry.register(contract(kind))
        options.setdefault("on_outcome", self.outcomes.append)
        bound = EffectBinding(provider, amount)
        self.effects = ReceiptedEffects(
            ContractedEffectStore(store, registry),
            self.signer,
            budget_id=BUDGET,
            bindings={"ship_order": bound, "send_email": bound, "issue_refund": bound},
            **options,
        )
        self.ran = Ran()

    @property
    def public_key(self) -> str:
        return self.signer.public_key_hex()

    def server(self, **middleware: Any) -> FastMCP[Any]:
        ran = self.ran
        middleware.setdefault("constitution", review_rules())
        mcp: FastMCP[Any] = FastMCP("shop", middleware=[ACGSMiddleware(effects=self.effects, **middleware)])

        @mcp.tool
        def ship_order(order_id: str) -> dict[str, Any]:
            """Ship an order (dispatched through the provider)."""
            ran.calls.append(("ship_order", {"order_id": order_id}))
            return {}

        @mcp.tool
        def send_email(to: str, body: str) -> dict[str, Any]:
            """Send an email (dispatched through the provider)."""
            ran.calls.append(("send_email", {"to": to, "body": body}))
            return {}

        @mcp.tool
        def issue_refund(order_id: str, amount: float) -> dict[str, Any]:
            """Refund an order (dispatched through the provider)."""
            ran.calls.append(("issue_refund", {"order_id": order_id, "amount": amount}))
            return {}

        @mcp.tool
        def delete_user(user_id: str) -> str:
            """An unbound side-effecting tool."""
            ran.calls.append(("delete_user", {"user_id": user_id}))
            return "deleted"

        @mcp.tool(annotations={"readOnlyHint": True})
        def get_order(order_id: str) -> str:
            """An unbound read-only tool."""
            ran.calls.append(("get_order", {"order_id": order_id}))
            return f"order {order_id}"

        return mcp


def key(value: object = "idem-key-0000001") -> dict[str, Any]:
    return {IDEMPOTENCY_KEY_META: value}


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "effects.sqlite3", FakeProvider())


async def test_a_call_denied_by_a_rule_never_reaches_the_provider(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="ACGS-006"):
            await client.call_tool("send_email", LEAK, meta=key())
    assert harness.provider.sends == []
    assert harness.ran.calls == []
    assert harness.outcomes == []


async def test_a_bound_call_is_sent_once_and_returns_signed_chained_receipts(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        result = await client.call_tool("ship_order", ORDER, meta=key())
    data = result.structured_content
    assert data is not None
    assert harness.ran.calls == []  # the provider is the effect channel; the tool body never runs
    assert len(harness.provider.sends) == 1
    assert data["outcome"] == "DONE"
    assert data["provider_reference"] is None
    assert data["effect_key"] == harness.provider.sends[0].key

    outcome = SignedOutcomeReceipt.from_dict(data["outcome_receipt"])
    assert outcome.verify(harness.public_key)
    assert outcome.receipt.outcome is EffectOutcome.DONE
    assert outcome.receipt.effect_key == data["effect_key"]
    assert outcome.receipt.provider_contract_digest == contract().digest
    assert outcome.receipt.grant_identity.startswith("fastmcp-acgs:grant:v2:")
    assert harness.outcomes == [outcome]

    decision = SignedReceipt.from_dict(data["decision_receipt"])
    assert decision.verify(harness.public_key)
    assert signed_envelope_identity(decision) == data["decision_receipt_identity"]
    assert outcome.receipt.decision_receipt_identity == data["decision_receipt_identity"]
    assert data["decision_receipt_identity"].startswith("acgs:signed-envelope:")
    assert receipt_identity(decision.receipt).startswith("acgs:receipt:v2:sha256:")
    assert decision.receipt.schema_version == RECEIPT_SCHEMA_V2
    assert decision.receipt.grant_id == decision.receipt.request_id
    assert decision.receipt.policy_digest == bind_policy(review_rules()).digest
    assert decision.receipt.method_id == "ship_order"
    assert decision.receipt.decision_type == "ALLOW"
    assert decision.receipt.matched_constraints == (BASELINE_CONSTRAINT_MARKER,)
    assert decision.receipt.proposed_method == "ship_order"
    assert decision.receipt.policy_version == review_rules().hash_versioned


async def test_a_retry_with_the_same_key_is_not_sent_again(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key())
        second = await client.call_tool("ship_order", ORDER, meta=key())
    assert len(harness.provider.sends) == 1
    assert first.structured_content == second.structured_content
    assert len(harness.outcomes) == 1


async def test_a_different_key_is_a_different_effect(harness: Harness) -> None:
    async with Client(harness.server()) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key("idem-key-0000001"))
        second = await client.call_tool("ship_order", ORDER, meta=key("idem-key-0000002"))
        third = await client.call_tool("ship_order", {"order_id": "B-2"}, meta=key("idem-key-0000001"))
    keys = {result.structured_content["effect_key"] for result in (first, second, third)}
    assert len(keys) == 3
    assert len(harness.provider.sends) == 3


@pytest.mark.parametrize("meta", [None, {}, key(""), key("   "), key(7)])
async def test_a_missing_idempotency_key_is_denied(harness: Harness, mode: str, meta: dict[str, Any] | None) -> None:
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="acgs/idempotency-key"):
            await client.call_tool("ship_order", ORDER, meta=meta)
    assert harness.provider.sends == []


async def test_an_in_process_call_has_no_idempotency_key_and_is_denied(harness: Harness) -> None:
    with pytest.raises(ToolError, match="acgs/idempotency-key"):
        await harness.server().call_tool("ship_order", ORDER)
    assert harness.provider.sends == []


@pytest.mark.parametrize("kind", [ProviderContractClass.BOUNDED, ProviderContractClass.UNBOUNDED, None])
async def test_a_provider_without_a_fenceable_contract_is_denied(
    tmp_path: Path, kind: ProviderContractClass | None
) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), kind=kind)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match="provider contract"):
            await client.call_tool("ship_order", ORDER, meta=key())
    assert harness.provider.sends == []
    assert harness.effects.store.store.budget(BUDGET).reserved == 0  # nothing was reserved


async def test_an_exhausted_budget_is_denied_without_leaking_store_internals(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), amount=101)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match=r"was not executed\. The effect store refused the effect") as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
    assert "capacity" not in str(raised.value)
    assert harness.provider.sends == []


async def test_a_store_error_before_dispatch_is_denied(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        msg = "disk on fire"
        raise ValueError(msg)

    monkeypatch.setattr(harness.effects.store, "send", broken)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match=r"was not executed\. The effect store refused the effect") as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
    assert "disk on fire" not in str(raised.value)  # internals stay in the server log
    assert harness.outcomes == []


async def test_not_done_emits_a_receipt_and_raises(tmp_path: Path, mode: str) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(outcome=EffectOutcome.NOT_DONE))
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="did not happen") as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
    [outcome] = harness.outcomes
    assert outcome.receipt.outcome is EffectOutcome.NOT_DONE
    assert outcome.verify(harness.public_key)
    assert outcome.receipt.identity in str(raised.value)


async def test_a_provider_timeout_is_held_without_a_receipt_and_never_retried(tmp_path: Path, mode: str) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(fail=True))
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="outcome unknown; held; reconcile with key") as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
        with pytest.raises(ToolError, match="outcome unknown; held"):
            await client.call_tool("ship_order", ORDER, meta=key())
    effect_key = harness.provider.sends[0].key
    assert effect_key in str(raised.value)
    assert len(harness.provider.sends) == 1
    assert harness.outcomes == []
    assert harness.effects.store.store.get(effect_key).state is EffectState.UNKNOWN


async def test_reconcile_after_a_restart_settles_the_effect_and_emits_a_receipt(tmp_path: Path) -> None:
    path = tmp_path / "effects.sqlite3"
    before = Harness(path, FakeProvider(fail=True))
    async with Client(before.server()) as client:
        with pytest.raises(ToolError, match="held"):
            await client.call_tool("ship_order", ORDER, meta=key())
    effect_key = before.provider.sends[0].key

    after = Harness(path, FakeProvider())  # a new process: new store object, registry and signer on the same file
    outcome = after.effects.reconcile(effect_key, "ship_order")
    assert outcome is not None
    assert outcome.receipt.outcome is EffectOutcome.DONE
    assert outcome.verify(after.public_key)
    assert after.outcomes == [outcome]
    assert len(after.provider.queries) == 1
    assert after.provider.sends == []
    assert outcome.receipt.decision_receipt_identity.startswith("acgs:signed-envelope:")

    assert after.effects.reconcile(effect_key, "ship_order") == outcome  # stored, not emitted again
    assert after.outcomes == [outcome]
    async with Client(after.server()) as client:
        retried = await client.call_tool("ship_order", ORDER, meta=key())
    assert retried.structured_content["outcome_receipt"] == outcome.to_dict()
    assert after.provider.sends == []


async def test_reconcile_of_a_still_unknown_effect_returns_none(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(fail=True, query_outcome=EffectOutcome.UNKNOWN))
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match="held"):
            await client.call_tool("ship_order", ORDER, meta=key())
    assert harness.effects.reconcile(harness.provider.sends[0].key, "ship_order") is None
    assert harness.outcomes == []


def test_reconcile_without_a_recorded_decision_is_refused(harness: Harness) -> None:
    store = harness.effects.store
    record = store.store.reserve(
        provider_id=PROVIDER, grant_identity="elsewhere", action={}, budget_id=BUDGET, amount=1
    )
    harness.provider.fail = True
    with pytest.raises(TimeoutError):
        store.send(record.key, harness.provider)
    with pytest.raises(ValueError, match="no decision is recorded"):
        harness.effects.reconcile(record.key, "ship_order")


async def test_an_unbound_side_effecting_tool_is_denied(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="no provider contract"):
            await client.call_tool("delete_user", {"user_id": "u1"})
        with pytest.raises(ToolError, match="no provider contract"):
            await client.call_tool("no_such_tool", {})
    assert harness.ran.calls == []


async def test_an_unbound_read_only_tool_runs(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        result = await client.call_tool("get_order", ORDER)
    assert result.data == "order A-17"
    assert harness.ran.names == ["get_order"]
    assert harness.provider.sends == []


async def test_unbound_tools_run_when_refusal_is_off(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), refuse_unbound_side_effects=False)
    async with Client(harness.server()) as client:
        await client.call_tool("delete_user", {"user_id": "u1"})
    assert harness.ran.names == ["delete_user"]


async def test_unreadable_annotations_deny(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(self: object, name: str, version: object = None) -> None:
        msg = "registry unavailable"
        raise RuntimeError(msg)

    mcp = harness.server()
    async with Client(mcp) as client:
        monkeypatch.setattr(FastMCP, "get_tool", broken)
        with pytest.raises(ToolError, match="no provider contract"):
            await client.call_tool("get_order", ORDER)
    assert harness.ran.calls == []


async def test_without_a_fastmcp_context_calls_are_denied(harness: Harness) -> None:
    async def call_next(context: Any) -> ToolResult:
        raise AssertionError

    decision = GovernanceDecision(verdict=Verdict.ALLOW, tool_name="ship_order", reason="ok")
    for name in ("ship_order", "get_order"):
        context = MiddlewareContext(message=mcp_types.CallToolRequestParams(name=name, arguments=ORDER))
        with pytest.raises(ToolError, match="was not executed"):
            await harness.effects.execute(context, call_next, decision, constitution=review_rules(), reviewed=False)


async def test_an_approved_review_is_also_sent_through_the_provider(harness: Harness, mode: str) -> None:
    mcp = harness.server(on_review="elicit")
    async with Client(mcp, mode=mode, elicitation_handler=answer("accept")) as client:
        result = await client.call_tool("issue_refund", REFUND, meta=key())
    data = result.structured_content
    assert data is not None
    assert harness.ran.calls == []
    assert len(harness.provider.sends) == 1
    decision = SignedReceipt.from_dict(data["decision_receipt"])
    assert decision.verify(harness.public_key)
    assert decision.receipt.decision_type == "ALLOW_WITH_CONTROLS"
    assert decision.receipt.required_controls == ("human_review",)
    assert decision.receipt.matched_constraints == ("REFUND-REVIEW",)
    assert len(harness.outcomes) == 1


async def test_a_declined_review_never_reaches_the_provider(harness: Harness) -> None:
    mcp = harness.server(on_review="elicit")
    async with Client(mcp, elicitation_handler=answer("decline")) as client:
        with pytest.raises(ToolError, match="did not approve"):
            await client.call_tool("issue_refund", REFUND, meta=key())
    assert harness.provider.sends == []


async def test_without_on_outcome_the_result_still_carries_the_receipt(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), on_outcome=None)
    async with Client(harness.server()) as client:
        result = await client.call_tool("ship_order", ORDER, meta=key())
    outcome = SignedOutcomeReceipt.from_dict(result.structured_content["outcome_receipt"])
    assert outcome.verify(harness.public_key)
    assert harness.outcomes == []


BINDING: dict[str, Any] = {"provider_id": PROVIDER, "budget_id": BUDGET, "amount": 1}


def test_grant_identity_is_stable_and_binds_the_call() -> None:
    same = grant_identity("t", "a" * 64, "k", **BINDING)
    assert same == grant_identity("t", "a" * 64, "k", **BINDING)
    assert same.startswith("fastmcp-acgs:grant:v2:")
    assert (
        len({same, grant_identity("t", "b" * 64, "k", **BINDING), grant_identity("t", "a" * 64, "j", **BINDING)}) == 3
    )


@pytest.mark.parametrize("change", [{"provider_id": "other"}, {"budget_id": "other"}, {"amount": 2}])
def test_grant_identity_binds_the_binding(change: dict[str, Any]) -> None:
    assert grant_identity("t", "a" * 64, "k", **BINDING) != grant_identity("t", "a" * 64, "k", **(BINDING | change))


def test_repr_and_validation(harness: Harness) -> None:
    effects = harness.effects
    assert repr(effects) == "ReceiptedEffects(budget_id='refunds', tools=['issue_refund', 'send_email', 'ship_order'])"
    bad: Any = object()
    with pytest.raises(TypeError, match="ContractedEffectStore"):
        ReceiptedEffects(bad, effects.signer, budget_id=BUDGET, bindings={})
    with pytest.raises(TypeError, match="ReceiptSigner"):
        ReceiptedEffects(effects.store, bad, budget_id=BUDGET, bindings={})
    with pytest.raises(ValueError, match="budget_id"):
        ReceiptedEffects(effects.store, effects.signer, budget_id=" ", bindings={})
    with pytest.raises(TypeError, match="EffectBinding"):
        ReceiptedEffects(effects.store, effects.signer, budget_id=BUDGET, bindings={"t": bad})
    with pytest.raises(TypeError, match="on_outcome"):
        ReceiptedEffects(effects.store, effects.signer, budget_id=BUDGET, bindings={}, on_outcome=bad)


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_invalid_amount_is_rejected(amount: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        EffectBinding(FakeProvider(), amount)


# Regression tests for the receipted-effects review findings. Each goes through the real Client and middleware.


@pytest.mark.parametrize("how", ["disabled", "unregistered", "unauthorized"])
async def test_a_bound_tool_fastmcp_would_not_run_is_refused_before_any_effect(harness: Harness, how: str) -> None:
    mcp = harness.server()
    name = "ship_order"
    if how == "disabled":
        mcp.disable(names={name})
    elif how == "unregistered":
        name = "ghost"
        harness.effects.bindings[name] = harness.effects.bindings["ship_order"]
    else:
        name = "admin_wipe"
        harness.effects.bindings[name] = harness.effects.bindings["ship_order"]

        @mcp.tool(auth=lambda ctx: False)
        def admin_wipe(order_id: str) -> dict[str, Any]:
            """A tool no caller is authorized for."""
            return {}

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="unknown, disabled, or not authorized"):
            await client.call_tool(name, ORDER, meta=key())
    assert harness.provider.sends == []
    assert harness.effects.store.store.budget(BUDGET).reserved == 0


@pytest.mark.parametrize(
    "arguments", [{"order_id": 12345}, {"order_id": "A-17", "extra": "anything"}, {}], ids=["type", "extra", "missing"]
)
async def test_bound_arguments_that_do_not_match_the_input_schema_are_refused(
    harness: Harness, mode: str, arguments: dict[str, Any]
) -> None:
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="arguments are not valid for the tool"):
            await client.call_tool("ship_order", arguments, meta=key())
    assert harness.provider.sends == []
    assert harness.effects.store.store.budget(BUDGET).reserved == 0


async def test_arguments_that_cannot_be_validated_are_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fn: object) -> None:
        msg = "no argument model"
        raise RuntimeError(msg)

    monkeypatch.setattr(effects_module, "TypeAdapter", broken)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", ORDER, meta=key())
    assert harness.provider.sends == []


async def test_a_receipt_failure_after_done_names_the_effect_and_a_same_key_retry_settles_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = legitimacy.sign_outcome
    calls = {"n": 0}

    def flaky(receipt: Any, signer: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "HSM unavailable"
            raise RuntimeError(msg)
        return real(receipt, signer)

    monkeypatch.setattr(legitimacy, "sign_outcome", flaky)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
        message = str(raised.value)
        effect_key = harness.provider.sends[0].key
        assert "was not executed" not in message
        assert f"effect {effect_key} is DONE" in message
        assert "same idempotency key" in message
        assert "HSM" not in message
        assert harness.effects.store.store.get(effect_key).state is EffectState.DONE
        assert harness.outcomes == []

        retried = await client.call_tool("ship_order", ORDER, meta=key())
    assert retried.structured_content["outcome"] == "DONE"
    assert len(harness.provider.sends) == 1  # the retry did not send again
    assert [outcome.receipt.effect_key for outcome in harness.outcomes] == [effect_key]


async def test_reconcile_signs_and_delivers_a_done_effect_whose_receipt_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = legitimacy.sign_outcome

    def broken(receipt: Any, signer: Any) -> Any:
        msg = "HSM unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(legitimacy, "sign_outcome", broken)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match="is DONE; its outcome receipt is not available yet"):
            await client.call_tool("ship_order", ORDER, meta=key())
    monkeypatch.setattr(legitimacy, "sign_outcome", real)
    outcome = harness.effects.reconcile(harness.provider.sends[0].key, "ship_order")
    assert outcome is not None
    assert outcome.receipt.outcome is EffectOutcome.DONE
    assert harness.outcomes == [outcome]
    assert harness.provider.queries == []  # a terminal record is not fenced again


async def test_an_unreadable_state_after_a_send_failure_is_not_reported_as_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(fail=True))

    real = harness.effects.store.store.get

    def locked(key: str) -> Any:
        if not harness.provider.sends:
            return real(key)
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(harness.effects.store.store, "get", locked)
    async with Client(harness.server()) as client:
        with pytest.raises(ToolError, match=r"could not be read\. Retry with the same idempotency key") as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
    assert "was not executed" not in str(raised.value)
    assert "locked" not in str(raised.value)
    assert len(harness.provider.sends) == 1


async def test_a_failing_on_outcome_neither_masks_done_nor_loses_the_receipt(tmp_path: Path, mode: str) -> None:
    delivered: list[SignedOutcomeReceipt] = []
    attempts = {"n": 0}

    def sink(outcome: SignedOutcomeReceipt) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            msg = "audit sink down"
            raise RuntimeError(msg)
        delivered.append(outcome)

    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), on_outcome=sink)
    async with Client(harness.server(), mode=mode) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key())
        assert first.structured_content["outcome"] == "DONE"  # not an opaque internal error
        assert delivered == []
        second = await client.call_tool("ship_order", ORDER, meta=key())  # the retry redelivers
        third = await client.call_tool("ship_order", ORDER, meta=key())  # once delivered, not again
    assert len(harness.provider.sends) == 1
    assert attempts["n"] == 2
    assert [outcome.to_dict() for outcome in delivered] == [second.structured_content["outcome_receipt"]]
    assert third.structured_content == second.structured_content


async def test_reconcile_redelivers_an_undelivered_receipt(tmp_path: Path) -> None:
    attempts: list[SignedOutcomeReceipt] = []

    def sink(outcome: SignedOutcomeReceipt) -> None:
        attempts.append(outcome)
        if len(attempts) == 1:
            msg = "audit sink down"
            raise RuntimeError(msg)

    harness = Harness(tmp_path / "effects.sqlite3", FakeProvider(), on_outcome=sink)
    async with Client(harness.server()) as client:
        result = await client.call_tool("ship_order", ORDER, meta=key())
    effect_key = result.structured_content["effect_key"]
    assert harness.effects.reconcile(effect_key, "ship_order") == attempts[0]
    assert len(attempts) == 2
    harness.effects.reconcile(effect_key, "ship_order")
    assert len(attempts) == 2  # delivered by the reconcile, so not again


def token(client_id: str, sub: str | None = None, *, claim: bool = False) -> AccessToken:
    if claim:
        return AccessToken(token="t", client_id=client_id, scopes=[], claims={"sub": sub})  # noqa: S106
    return AccessToken(token="t", client_id=client_id, scopes=[], subject=sub)  # noqa: S106


async def test_the_same_key_from_different_callers_is_a_different_effect(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers = iter([token("shop", "alice"), token("shop", "bob"), token("shop", "alice", claim=True)])
    monkeypatch.setattr(effects_module, "get_access_token", lambda: next(callers))
    mcp = harness.server()
    async with Client(mcp) as alice, Client(mcp) as bob:
        first = await alice.call_tool("ship_order", ORDER, meta=key("idem-retry-000001"))
        second = await bob.call_tool("ship_order", ORDER, meta=key("idem-retry-000001"))
        again = await alice.call_tool("ship_order", ORDER, meta=key("idem-retry-000001"))
    assert len(harness.provider.sends) == 2  # bob's effect is not collapsed into alice's
    assert first.structured_content["effect_key"] != second.structured_content["effect_key"]
    assert first.structured_content["outcome_receipt"] != second.structured_content["outcome_receipt"]
    assert again.structured_content == first.structured_content  # alice's retry is still deduplicated
    decision = SignedReceipt.from_dict(second.structured_content["decision_receipt"])
    assert decision.receipt.execution_boundary.allowed_subjects == ('["shop",null,"bob"]',)


def test_grant_identity_binds_the_subject() -> None:
    anonymous = grant_identity("t", "a" * 64, "k", **BINDING)
    assert anonymous == grant_identity("t", "a" * 64, "k", subject=None, **BINDING)
    assert (
        len(
            {
                anonymous,
                grant_identity("t", "a" * 64, "k", subject="x", **BINDING),
                grant_identity("t", "a" * 64, "k", subject="y", **BINDING),
            }
        )
        == 3
    )


async def test_an_unauthenticated_token_without_a_subject_still_scopes_by_client(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    callers = iter([token("app-1", claim=True), token("app-2", claim=True)])
    monkeypatch.setattr(effects_module, "get_access_token", lambda: next(callers))
    async with Client(harness.server()) as client:
        await client.call_tool("ship_order", ORDER, meta=key())
        await client.call_tool("ship_order", ORDER, meta=key())
    assert len(harness.provider.sends) == 2


class SlowProvider(FakeProvider):
    """A provider whose send blocks until released, so a duplicate arrives while the first is IN_FLIGHT."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def send(self, request: EffectRequest) -> ProviderResult:
        self.sends.append(request)
        if not self.release.wait(timeout=10):
            msg = "never released"
            raise TimeoutError(msg)
        return ProviderResult(request.key, self.provider_id, EffectOutcome.DONE, fenced=False)


async def test_a_concurrent_duplicate_while_in_flight_says_retry_not_reconcile(tmp_path: Path) -> None:
    harness = Harness(tmp_path / "effects.sqlite3", SlowProvider())
    provider = harness.provider
    assert isinstance(provider, SlowProvider)
    results: list[Any] = []
    async with Client(harness.server()) as client:

        async def first() -> None:
            results.append(await client.call_tool("ship_order", ORDER, meta=key()))

        async with anyio.create_task_group() as group:
            group.start_soon(first)
            with anyio.fail_after(10):
                while not provider.sends:
                    await anyio.sleep(0.01)
            with pytest.raises(ToolError) as raised:
                await client.call_tool("ship_order", ORDER, meta=key())
            provider.release.set()
        message = str(raised.value)
        assert f"effect {provider.sends[0].key} is in flight" in message
        assert "same idempotency key" in message
        assert "reconcile with key" not in message
        assert "outcome unknown" not in message
        again = await client.call_tool("ship_order", ORDER, meta=key())
    assert results[0].structured_content["outcome"] == "DONE"
    assert again.structured_content == results[0].structured_content
    assert len(provider.sends) == 1


def _email(value: str) -> str:
    if "@" not in value:
        msg = "not an email address"
        raise ValueError(msg)
    return value


Email = Annotated[str, AfterValidator(_email)]


def typed_server(harness: Harness, *, currency: str = "USD", strict: bool | None = None) -> FastMCP[Any]:
    """Bound tools whose argument model has validators, formats and defaults the JSON schema alone does not check."""
    mcp: FastMCP[Any] = FastMCP(
        "typed",
        middleware=[ACGSMiddleware(effects=harness.effects, constitution=review_rules())],
        strict_input_validation=strict,
    )

    @mcp.tool
    def send_email(to: Email, body: datetime) -> dict[str, Any]:
        """Send an email (dispatched through the provider)."""
        return {"to": to, "body": body}

    @mcp.tool
    def ship_order(order_id: str, quantity: int = 1, currency: str = currency) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "quantity": quantity, "currency": currency}

    return mcp


@pytest.mark.parametrize(
    "arguments",
    [
        {"to": "not-an-email", "body": "2026-10-29T12:00:00Z"},
        {"to": "a@example.com", "body": "garbage"},
    ],
    ids=["validator", "datetime"],
)
async def test_the_tool_argument_model_is_enforced_before_any_effect(
    harness: Harness, mode: str, arguments: dict[str, Any]
) -> None:
    plain = FastMCP[Any]("plain")
    plain.add_tool(await typed_server(harness).get_tool("send_email"))  # type: ignore[arg-type]
    async with Client(plain, mode=mode) as client:  # plain FastMCP rejects them too
        assert (await client.call_tool("send_email", arguments, raise_on_error=False)).is_error
    async with Client(typed_server(harness), mode=mode) as client:
        with pytest.raises(ToolError, match=r"was not executed\. The arguments are not valid"):
            await client.call_tool("send_email", arguments, meta=key())
    assert harness.provider.sends == []
    assert harness.effects.store.store.budget(BUDGET).reserved == 0


async def test_the_provider_receives_the_validated_and_coerced_arguments(harness: Harness) -> None:
    async with Client(typed_server(harness)) as client:
        await client.call_tool("send_email", {"to": "a@example.com", "body": "2026-10-29T12:00:00+00:00"}, meta=key())
        await client.call_tool("ship_order", {"order_id": "A-17", "quantity": "3"}, meta=key("idem-key-0000002"))
    email, order = (json.loads(request.action_json) for request in harness.provider.sends)
    assert email["arguments"] == {"to": "a@example.com", "body": "2026-10-29T12:00:00Z"}
    assert order["arguments"] == {"order_id": "A-17", "quantity": 3, "currency": "USD"}


async def test_arguments_that_coerce_to_the_same_values_are_the_same_effect(harness: Harness, mode: str) -> None:
    async with Client(typed_server(harness), mode=mode) as client:
        first = await client.call_tool("ship_order", {"order_id": "A-17", "quantity": 3}, meta=key())
        second = await client.call_tool("ship_order", {"order_id": "A-17", "quantity": "3"}, meta=key())
    assert len(harness.provider.sends) == 1
    assert first.structured_content["effect_key"] == second.structured_content["effect_key"]
    decision = SignedReceipt.from_dict(first.structured_content["decision_receipt"])
    expected = _call_digest("ship_order", {"order_id": "A-17", "quantity": 3})
    assert decision.receipt.execution_boundary.allowed_scope == f"call-digest:sha256:{expected}"


async def test_strict_input_validation_is_honoured(harness: Harness) -> None:
    async with Client(typed_server(harness, strict=True)) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": "A-17", "quantity": "3"}, meta=key())
        await client.call_tool("send_email", {"to": "a@example.com", "body": "2026-10-29T12:00:00Z"}, meta=key())
    assert len(harness.provider.sends) == 1


class SchemaOnlyTool(Tool):
    """A tool with an input schema but no pydantic argument model (not a FunctionTool)."""

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(content="sent")


async def test_a_tool_without_an_argument_model_is_checked_against_its_schema_formats(harness: Harness) -> None:
    mcp = typed_server(harness)
    mcp.local_provider.remove_tool("send_email")
    schema = {"type": "object", "properties": {"to": {"type": "string", "format": "email"}}, "required": ["to"]}
    mcp.add_tool(SchemaOnlyTool(name="send_email", parameters=schema))
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("send_email", {"to": "not-an-email"}, meta=key())
        await client.call_tool("send_email", {"to": "a@example.com"}, meta=key())
    assert len(harness.provider.sends) == 1


async def test_a_schema_only_tool_does_not_coerce_so_a_mistyped_argument_is_refused(harness: Harness) -> None:
    # jsonschema validates without coercing: "3" is not an integer, so it never reaches the effect path as 3.
    mcp = typed_server(harness)
    mcp.local_provider.remove_tool("ship_order")
    schema = {"type": "object", "properties": {"quantity": {"type": "integer"}}, "required": ["quantity"]}
    mcp.add_tool(SchemaOnlyTool(name="ship_order", parameters=schema))
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"quantity": "3"}, meta=key())
        await client.call_tool("ship_order", {"quantity": 3}, meta=key())
        await client.call_tool("ship_order", {"quantity": 3}, meta=key())
    assert len(harness.provider.sends) == 1


def rebound(harness: Harness, **change: Any) -> ReceiptedEffects:
    """The same store and signer, with every tool rebound by the operator."""
    binding = EffectBinding(change.pop("provider", harness.provider), change.pop("amount", 1))
    return ReceiptedEffects(
        harness.effects.store,
        harness.signer,
        budget_id=BUDGET,
        bindings={"ship_order": binding},
        on_outcome=harness.outcomes.append,
    )


async def test_a_changed_amount_is_a_new_grant_not_a_false_refusal(harness: Harness) -> None:
    async with Client(harness.server()) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key())
    harness.effects = rebound(harness, amount=2)
    async with Client(harness.server()) as client:
        second = await client.call_tool("ship_order", ORDER, meta=key())
    assert first.structured_content["effect_key"] != second.structured_content["effect_key"]
    assert len(harness.provider.sends) == 2
    assert harness.effects.store.store.budget(BUDGET).settled == 3


async def test_a_same_key_retry_whose_effect_changed_says_it_may_already_have_happened(harness: Harness) -> None:
    async with Client(typed_server(harness)) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key())
    effect_key = first.structured_content["effect_key"]
    async with Client(typed_server(harness, currency="EUR")) as client:  # the tool's default changed
        with pytest.raises(ToolError) as raised:
            await client.call_tool("ship_order", ORDER, meta=key())
    message = str(raised.value)
    assert "was not executed" not in message
    assert f"effect {effect_key} is already reserved" in message
    assert "may already have happened" in message
    assert len(harness.provider.sends) == 1


async def test_a_conflict_whose_lookup_fails_still_says_it_may_have_happened(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Client(typed_server(harness)) as client:
        await client.call_tool("ship_order", ORDER, meta=key())

    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(harness.effects, "_connect", locked)
    async with Client(typed_server(harness, currency="EUR")) as client:
        with pytest.raises(ToolError, match="its key could not be read; it may already have happened"):
            await client.call_tool("ship_order", ORDER, meta=key())
    assert len(harness.provider.sends) == 1


async def test_the_same_subject_from_different_issuers_is_a_different_effect(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def issued(issuer: str) -> AccessToken:
        return AccessToken(token="t", client_id="shop", scopes=[], subject="alice", claims={"iss": issuer})  # noqa: S106

    callers = iter([issued("https://a.example"), issued("https://b.example")])
    monkeypatch.setattr(effects_module, "get_access_token", lambda: next(callers))
    async with Client(harness.server()) as client:
        first = await client.call_tool("ship_order", ORDER, meta=key())
        second = await client.call_tool("ship_order", ORDER, meta=key())
    assert len(harness.provider.sends) == 2
    decision = SignedReceipt.from_dict(second.structured_content["decision_receipt"])
    assert decision.receipt.execution_boundary.allowed_subjects == ('["shop","https://b.example","alice"]',)
    assert first.structured_content["effect_key"] != second.structured_content["effect_key"]


async def test_unresolvable_type_hints_fall_back_to_the_signature_annotations(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unresolvable(fn: object, **kwargs: object) -> None:
        msg = "unresolvable forward reference"
        raise NameError(msg)

    async with Client(typed_server(harness)) as client:
        monkeypatch.setattr(effects_module, "get_type_hints", unresolvable)
        await client.call_tool("ship_order", {"order_id": "A-17", "quantity": "3"}, meta=key())
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": "A-17", "quantity": "x"}, meta=key("idem-key-0000002"))
    assert json.loads(harness.provider.sends[0].action_json)["arguments"]["quantity"] == 3


async def test_an_injected_context_parameter_is_not_an_argument(harness: Harness, mode: str) -> None:
    mcp: FastMCP[Any] = FastMCP("ctx", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(order_id: str, ctx: Context) -> dict[str, Any]:
        """A bound tool with an injected context (dispatched through the provider)."""
        return {"order_id": order_id, "server": ctx.fastmcp.name}

    async with Client(mcp, mode=mode) as client:
        result = await client.call_tool("ship_order", ORDER, meta=key())
    assert result.structured_content["outcome"] == "DONE"
    assert json.loads(harness.provider.sends[0].action_json)["arguments"] == ORDER


def keyed_server(harness: Harness) -> FastMCP[Any]:
    """A server whose bound ``ship_order`` declares the idempotency key as an argument the model fills in."""
    mcp: FastMCP[Any] = FastMCP("keyed", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(order_id: str, idempotency_key: str) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "key": idempotency_key}

    return mcp


ARG_KEY = "0b6a1f3e-5c2d-4e8f-9a7b-1c2d3e4f5a6b"


def keyed(value: str = ARG_KEY, **arguments: Any) -> dict[str, Any]:
    return {**ORDER, **arguments, IDEMPOTENCY_ARGUMENT: value}


def sent_arguments(harness: Harness) -> list[dict[str, Any]]:
    return [json.loads(send.action_json)["arguments"] for send in harness.provider.sends]


async def test_an_argument_key_works_without_meta(harness: Harness, mode: str) -> None:
    async with Client(keyed_server(harness), mode=mode) as client:
        result = await client.call_tool("ship_order", keyed())
    assert result.structured_content["outcome"] == "DONE"
    assert sent_arguments(harness) == [ORDER]  # the key is never provider business data
    assert len(harness.outcomes) == 1


async def test_a_retry_with_the_same_argument_key_is_not_sent_again(harness: Harness, mode: str) -> None:
    async with Client(keyed_server(harness), mode=mode) as client:
        first = await client.call_tool("ship_order", keyed())
        second = await client.call_tool("ship_order", keyed())
    assert len(harness.provider.sends) == 1
    assert first.structured_content == second.structured_content


async def test_an_equal_meta_and_argument_key_is_the_same_effect_as_either_alone(harness: Harness) -> None:
    async with Client(keyed_server(harness)) as client:
        both = await client.call_tool("ship_order", keyed(), meta=key(ARG_KEY))
        argument = await client.call_tool("ship_order", keyed())
    assert len(harness.provider.sends) == 1
    assert both.structured_content["effect_key"] == argument.structured_content["effect_key"]


async def test_a_meta_key_differing_from_the_argument_key_is_denied(harness: Harness, mode: str) -> None:
    async with Client(keyed_server(harness), mode=mode) as client:
        with pytest.raises(ToolError, match=r"was not executed\. The idempotency key in _meta .* differ"):
            await client.call_tool("ship_order", keyed(), meta=key())
    assert harness.provider.sends == []


@pytest.mark.parametrize("bad", ["short-key", "a" * 15, "a" * 129, "a" * 15 + "!", "a" * 16 + "\n", "ключ" * 5])
async def test_a_malformed_key_is_denied(harness: Harness, bad: str) -> None:
    async with Client(keyed_server(harness)) as client:
        with pytest.raises(ToolError, match="16 to 128 characters"):
            await client.call_tool("ship_order", keyed(bad))
        with pytest.raises(ToolError, match="16 to 128 characters"):
            await client.call_tool("ship_order", ORDER, meta=key(bad))
    assert harness.provider.sends == []


async def test_the_key_boundaries_are_accepted(harness: Harness) -> None:
    async with Client(keyed_server(harness)) as client:
        await client.call_tool("ship_order", keyed("a" * 16))
        await client.call_tool("ship_order", keyed("A_-9" * 32))
    assert len(harness.provider.sends) == 2


async def test_calls_differing_only_in_the_argument_key_are_distinct_effects(harness: Harness) -> None:
    async with Client(keyed_server(harness)) as client:
        first = await client.call_tool("ship_order", keyed("idem-key-0000001"))
        second = await client.call_tool("ship_order", keyed("idem-key-0000002"))
    assert first.structured_content["effect_key"] != second.structured_content["effect_key"]
    assert sent_arguments(harness) == [ORDER, ORDER]
    assert all(IDEMPOTENCY_ARGUMENT not in json.loads(send.action_json)["arguments"] for send in harness.provider.sends)


async def test_the_key_argument_is_not_part_of_the_signed_call_digest(harness: Harness) -> None:
    async with Client(keyed_server(harness)) as client:
        result = await client.call_tool("ship_order", keyed())
    decision = SignedReceipt.from_dict(result.structured_content["decision_receipt"])
    expected = _call_digest("ship_order", ORDER)
    assert decision.receipt.execution_boundary.allowed_scope == f"call-digest:sha256:{expected}"


async def test_an_undeclared_key_argument_is_rejected(harness: Harness, mode: str) -> None:
    async with Client(harness.server(), mode=mode) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", keyed(), meta=key())
        with pytest.raises(ToolError, match="acgs/idempotency-key"):
            await client.call_tool("ship_order", keyed())
    assert harness.provider.sends == []


async def test_a_required_key_argument_needs_the_argument_and_an_optional_one_accepts_meta(harness: Harness) -> None:
    optional: FastMCP[Any] = FastMCP("optional", middleware=[ACGSMiddleware(effects=harness.effects)])

    @optional.tool
    def ship_order(order_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "key": idempotency_key}

    async with Client(keyed_server(harness)) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", ORDER, meta=key(ARG_KEY))
    async with Client(optional) as client:
        by_meta = await client.call_tool("ship_order", ORDER, meta=key(ARG_KEY))
        by_argument = await client.call_tool("ship_order", keyed())
    assert by_meta.structured_content["effect_key"] == by_argument.structured_content["effect_key"]
    assert sent_arguments(harness) == [ORDER]


def aliased_server(harness: Harness) -> FastMCP[Any]:
    """A bound ``ship_order`` whose quantity is sent under a pydantic alias."""
    mcp: FastMCP[Any] = FastMCP("aliased", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(order_id: str, quantity: Annotated[int, Field(alias="qty")] = 1) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "quantity": quantity}

    return mcp


async def test_an_aliased_argument_is_part_of_the_call_digest(harness: Harness, mode: str) -> None:
    async with Client(aliased_server(harness), mode=mode) as client:
        small = await client.call_tool("ship_order", {"order_id": "A-17", "qty": 3}, meta=key())
        large = await client.call_tool("ship_order", {"order_id": "A-17", "qty": 999}, meta=key("idem-key-0000002"))
    assert sent_arguments(harness) == [{"order_id": "A-17", "quantity": 3}, {"order_id": "A-17", "quantity": 999}]
    decision = SignedReceipt.from_dict(small.structured_content["decision_receipt"])
    expected = _call_digest("ship_order", {"order_id": "A-17", "quantity": 3})
    assert decision.receipt.execution_boundary.allowed_scope == f"call-digest:sha256:{expected}"
    assert small.structured_content["effect_key"] != large.structured_content["effect_key"]


async def test_an_alias_the_digest_cannot_map_is_refused(harness: Harness) -> None:
    mcp: FastMCP[Any] = FastMCP("choices", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(
        order_id: str, quantity: Annotated[int, Field(validation_alias=AliasChoices("qty", "n"))] = 1
    ) -> str:
        """Ship an order (dispatched through the provider)."""
        return order_id

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": "A-17", "qty": 3}, meta=key())
    assert harness.provider.sends == []


class Box(BaseModel):
    labels: frozenset[str]


SET_DIGEST = """
import sys
from typing import Any
from pydantic import BaseModel
from fastmcp.tools import Tool
from fastmcp_acgs import effects

class Box(BaseModel):
    labels: frozenset[str]

def tag(tags: frozenset[str], box: Box) -> None: ...

tool = Tool.from_function(tag)
labels = sys.argv[1:]
arguments: Any = effects._validated_arguments(tool, {"tags": labels, "box": {"labels": labels}}, strict=False)
print(effects._call_digest("tag", arguments))
"""


def test_a_set_argument_has_the_same_digest_in_every_process() -> None:
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    digests = {
        subprocess.run(  # noqa: S603 - the interpreter and a constant script
            [sys.executable, "-c", SET_DIGEST, *words],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in range(1, 6)
    }
    assert len(digests) == 1


def test_sets_are_ordered_inside_models_dataclasses_and_containers() -> None:
    @dataclasses.dataclass
    class Pair:
        tags: set[str]

    value = {"box": Box(labels=frozenset({"b", "a"})), "pair": Pair({"z", "y"}), "nested": ({2, 1},)}
    assert effects_module._ordered_sets(value, to_jsonable_python(value)) == {
        "box": {"labels": ["a", "b"]},
        "pair": {"tags": ["y", "z"]},
        "nested": [[1, 2]],
    }


async def test_an_alias_given_as_the_parameter_default_is_mapped(harness: Harness) -> None:
    mcp: FastMCP[Any] = FastMCP("defaults", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(
        order_id: Annotated[str, Field(description="The order.")], quantity: int = Field(1, alias="qty")
    ) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "quantity": quantity}

    async with Client(mcp) as client:
        await client.call_tool("ship_order", {"order_id": "A-17", "qty": 3}, meta=key())
        await client.call_tool("ship_order", {"order_id": "A-17", "qty": 4}, meta=key("idem-key-0000002"))
    assert sent_arguments(harness) == [{"order_id": "A-17", "quantity": 3}, {"order_id": "A-17", "quantity": 4}]


async def test_parameters_that_cannot_be_read_are_refused(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fn: object) -> None:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(effects_module, "_parameters", broken)
    async with Client(aliased_server(harness)) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": "A-17", "qty": 3}, meta=key())
    assert harness.provider.sends == []


class Stamped(BaseModel):
    when: str
    secret: str = Field(default="hidden", exclude=True)
    tags: frozenset[str]

    @field_serializer("when", when_used="json")
    def _stamp(self, when: str) -> str:
        return f"JSON:{when}"


class Joined(BaseModel):
    tags: frozenset[str]

    @field_serializer("tags")
    def _join(self, tags: frozenset[str]) -> str:
        return ",".join(tags)


class Hidden(BaseModel):
    tags: frozenset[str] = Field(exclude=True)


class Flags(enum.Enum):
    BOTH = frozenset({"b", "a"})


def test_custom_json_serializers_keep_their_output_while_sets_are_ordered() -> None:
    value = {"stamped": Stamped(when="w", tags=frozenset({"b", "a"})), "flags": Flags.BOTH}
    assert effects_module._ordered_sets(value, to_jsonable_python(value)) == {
        "stamped": {"when": "JSON:w", "tags": ["a", "b"]},
        "flags": ["a", "b"],
    }


@pytest.mark.parametrize(
    "value",
    [Joined(tags=frozenset({"b", "a"})), Hidden(tags=frozenset({"b", "a"}))],
    ids=["serializer", "excluded"],
)
def test_a_set_whose_serialized_order_cannot_be_fixed_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="cannot be made canonical"):
        effects_module._ordered_sets(value, to_jsonable_python(value))


async def test_an_alias_that_is_another_parameter_name_is_refused(harness: Harness) -> None:
    mcp: FastMCP[Any] = FastMCP("collide", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(order_id: str = "A-17", quantity: Annotated[int, Field(alias="order_id")] = 1) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id, "quantity": quantity}

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": 7}, meta=key())
    assert harness.provider.sends == []


async def test_an_aliased_idempotency_key_argument_is_refused(harness: Harness) -> None:
    mcp: FastMCP[Any] = FastMCP("aliased-key", middleware=[ACGSMiddleware(effects=harness.effects)])

    @mcp.tool
    def ship_order(order_id: str, idempotency_key: Annotated[str | None, Field(alias="idem")] = None) -> dict[str, Any]:
        """Ship an order (dispatched through the provider)."""
        return {"order_id": order_id}

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="arguments are not valid"):
            await client.call_tool("ship_order", {"order_id": "A-17", "idem": ARG_KEY}, meta=key())
    assert harness.provider.sends == []


def test_a_set_is_found_however_deeply_it_is_nested() -> None:
    @dataclasses.dataclass
    class Pair:
        tags: tuple[str, ...]

    assert effects_module._has_set([{"k": (Pair(("a",)), Stamped(when="w", tags=frozenset()))}])
    assert effects_module._has_set({"k": [Flags.BOTH]})
    assert not effects_module._has_set([{"k": (Pair(("a",)), 1)}])
