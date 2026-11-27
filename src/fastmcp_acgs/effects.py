# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Receipted effects: dispatch a governed tool's side effect through a provider contract, at most once.

Experimental. Uses the ``acgs-lite>=4.0.0`` provider-contract API (``acgs_lite.legitimacy.ProviderContract`` and
friends). This module imports none of it at module scope; constructing :class:`ReceiptedEffects` without that API
raises ``ImportError``.

For a tool with an :class:`EffectBinding`, ``ACGSMiddleware`` does not run the tool body. After the ACGS decision
allows the call (or a reviewer approves it), it resolves the tool through FastMCP (so a disabled, unknown or
unauthorized tool is refused), validates and coerces the arguments as FastMCP would for the tool body, signs a
decision receipt, reserves the effect in the ``EffectStore`` under a key derived from the caller, the call, the
idempotency key and the binding, and sends it once through the bound provider. A terminal outcome yields a
signed outcome receipt chained to the decision receipt. The ``EffectStore`` is evidence, not authority: the ACGS
decision comes first and is the only authorization.

The idempotency key comes from the request ``_meta`` under ``acgs/idempotency-key`` or, for a tool that declares
it, from a tool argument named ``idempotency_key`` that the model fills in (off-the-shelf MCP clients send no custom
``_meta``). If both are present they must be equal. The key must be 16 to 128 characters from ``A-Z a-z 0-9 _ -``
(a UUIDv4 fits). The argument is removed from the call digest and from the action sent to the provider, so it is
never business data and the same call keyed either way maps to the same effect.

Middleware below ``ACGSMiddleware`` and tool-call interceptors do not run for bound tools, because the chain is not
continued: the provider performs the effect instead of the tool body.

Example:
    A server whose ``issue_refund`` tool is dispatched through a FENCEABLE payment provider::

        registry = ProviderContractRegistry()
        registry.register(payments_contract)
        store = EffectStore("effects.sqlite3")
        store.create_budget("refunds", 1000)
        effects = ReceiptedEffects(
            ContractedEffectStore(store, registry),
            Ed25519ReceiptSigner.generate(),
            budget_id="refunds",
            bindings={"issue_refund": EffectBinding(payments_provider)},
        )
        mcp = FastMCP("shop", middleware=[ACGSMiddleware(effects=effects)])
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args, get_type_hints

import anyio
import mcp_types
from acgs_lite import Constitution
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, without_injected_parameters
from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.tools import FunctionTool, Tool, ToolResult
from jsonschema.validators import validator_for
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import to_json, to_jsonable_python

from fastmcp_acgs._governance import GovernanceDecision
from fastmcp_acgs.middleware import _call_digest

if TYPE_CHECKING:
    from acgs_lite.legitimacy import (
        ContractedEffectStore,
        EffectProvider,
        EffectRecord,
        EffectState,
        ReceiptSigner,
        SignedOutcomeReceipt,
        SignedReceipt,
    )

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_META = "acgs/idempotency-key"
"""Request ``_meta`` key under which a client sends the idempotency key of a side-effecting call."""

IDEMPOTENCY_ARGUMENT = "idempotency_key"
"""Name of the tool argument that carries the idempotency key, for a bound tool that declares it (``str``)."""

_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")

GRANT_DOMAIN = "fastmcp-acgs-grant-v2"
"""Domain separator of the grant identity that keys an effect in the ``EffectStore``."""

_INSTALL_HINT = (
    "Receipted effects need the acgs-lite provider-contract API (acgs_lite.legitimacy.ProviderContract), "
    "available in acgs-lite>=4.0.0. Upgrade with: pip install 'acgs-lite>=4.0.0,<5'"
)
_DECISIONS_TABLE = "fastmcp_acgs_effect_decisions_v2"
_HUMAN_REVIEW = "human_review"

_UNRESOLVED = "The tool is unknown, disabled, or not authorized for this caller."
_BAD_ARGUMENTS = "The arguments are not valid for the tool."
_CONTRACT_REFUSED = "The provider contract or decision receipt was refused (fail-closed)."
_STORE_REFUSED = "The effect store refused the effect (fail-closed)."
_NO_SIDE_EFFECT_CONTRACT = "The side-effecting tool has no provider contract (it is not readOnlyHint)."
_NO_KEY = (
    f"A side-effecting call needs an idempotency key in _meta under '{IDEMPOTENCY_KEY_META}' or, if the tool "
    f"declares it, in the '{IDEMPOTENCY_ARGUMENT}' argument: 16 to 128 characters from A-Z a-z 0-9 _ - "
    "(for example a UUIDv4)."
)
_KEY_MISMATCH = (
    f"The idempotency key in _meta ('{IDEMPOTENCY_KEY_META}') and in the '{IDEMPOTENCY_ARGUMENT}' argument differ."
)


def _require_a4() -> None:
    try:
        from acgs_lite.legitimacy import ContractedEffectStore, ProviderContract, sign_outcome  # noqa: F401, PLC0415
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc


@dataclass(frozen=True)
class EffectBinding:
    """How one tool's side effect is dispatched.

    Attributes:
        provider: The trusted provider adapter that performs the effect. It must have a FENCEABLE contract in the
            store's registry, or every call to the tool is denied.
        amount: Budget units one call reserves.
    """

    provider: EffectProvider
    amount: int = 1

    def __post_init__(self) -> None:
        """Check the amount.

        Raises:
            ValueError: If ``amount`` is not a positive integer.
        """
        if type(self.amount) is not int or self.amount <= 0:
            msg = "amount must be a positive integer."
            raise ValueError(msg)


def grant_identity(  # noqa: PLR0913
    tool_name: str,
    call_digest: str,
    idempotency_key: str,
    *,
    subject: str | None = None,
    provider_id: str,
    budget_id: str,
    amount: int,
) -> str:
    """The grant identity of one call: a sha256 over the caller, the call, the idempotency key and the binding.

    A retry of the same call with the same key by the same caller, under the same binding, maps to the same effect,
    so the ``EffectStore`` deduplicates it. The same key with different arguments, from a different authenticated
    caller, or after the operator changed the tool's provider, budget or amount, maps to a different effect.
    Unauthenticated callers (``subject=None``) share one namespace, so their keys must be globally unique (for
    example UUIDv4).

    Args:
        tool_name: The tool name.
        call_digest: The sha256 digest of the tool name and arguments.
        idempotency_key: The client-supplied idempotency key.
        subject: The authenticated caller, or None for an unauthenticated one.
        provider_id: The bound provider.
        budget_id: The budget the call reserves from.
        amount: The budget units the call reserves.

    Returns:
        ``fastmcp-acgs:grant:v2:<sha256 hex>``.
    """
    canonical = json.dumps(
        [GRANT_DOMAIN, subject, tool_name, call_digest, idempotency_key, provider_id, budget_id, amount],
        separators=(",", ":"),
    )
    return "fastmcp-acgs:grant:v2:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Settled:
    record: EffectRecord
    decision_identity: str
    signed_decision: dict[str, Any]
    outcome: SignedOutcomeReceipt
    delivered: bool


class ReceiptedEffects:
    """Dispatch the side effects of bound tools through provider contracts, with signed receipts.

    Pass an instance to ``ACGSMiddleware(effects=...)``. The middleware still decides every call first; this
    object only runs calls the middleware has allowed or a reviewer has approved.

    The mapping from effect key to the decision that caused it is kept in a table of the store's SQLite file, so
    :meth:`reconcile` works after a restart. The first decision recorded for an effect key wins: a retry's new
    decision receipt is not chained to an effect that an earlier decision already reserved.

    Outcome receipts reach ``on_outcome`` at least once: a receipt counts as delivered only after ``on_outcome``
    returns, so one that failed, or was interrupted by a crash or cancellation, is delivered again by the next retry
    of the call or by :meth:`reconcile`. A sink must deduplicate by ``receipt.identity``.
    """

    def __init__(  # noqa: PLR0913
        self,
        store: ContractedEffectStore,
        signer: ReceiptSigner,
        *,
        budget_id: str,
        bindings: Mapping[str, EffectBinding],
        on_outcome: Callable[[SignedOutcomeReceipt], None] | None = None,
        refuse_unbound_side_effects: bool = True,
    ) -> None:
        """Create the effects dispatcher.

        Args:
            store: The contracted effect store. Its registry decides which providers may be dispatched.
            signer: Signs decision receipts and outcome receipts (for example ``Ed25519ReceiptSigner``).
            budget_id: The ``EffectStore`` budget every bound call reserves from. It must already exist.
            bindings: Tool name to :class:`EffectBinding`.
            on_outcome: Called, from a worker thread, with each signed outcome receipt (DONE or NOT_DONE), at least
                once. Never called for UNKNOWN. An exception it raises is logged and never fails the call.
            refuse_unbound_side_effects: Deny calls to tools without a binding unless the tool's MCP annotations
                set ``readOnlyHint`` to true.

        Raises:
            ImportError: If the installed acgs-lite lacks the provider-contract API.
            TypeError: If an argument has the wrong type.
            ValueError: If ``budget_id`` is empty.
        """
        _require_a4()
        from acgs_lite.legitimacy import ContractedEffectStore, ReceiptSigner  # noqa: PLC0415

        if not isinstance(store, ContractedEffectStore):
            msg = "store must be a ContractedEffectStore."
            raise TypeError(msg)
        if not isinstance(signer, ReceiptSigner):
            msg = "signer must be a ReceiptSigner."
            raise TypeError(msg)
        if not isinstance(budget_id, str) or not budget_id.strip():
            msg = "budget_id must be a non-empty string."
            raise ValueError(msg)
        if not all(isinstance(binding, EffectBinding) for binding in bindings.values()):
            msg = "bindings must map tool names to EffectBinding."
            raise TypeError(msg)
        if on_outcome is not None and not callable(on_outcome):
            msg = "on_outcome must be callable."
            raise TypeError(msg)
        self.store = store
        self.signer = signer
        self.budget_id = budget_id
        self.bindings: dict[str, EffectBinding] = dict(bindings)
        self.on_outcome = on_outcome
        self.refuse_unbound_side_effects = refuse_unbound_side_effects
        self._path: Path = store.store.path
        with self._connect() as db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS {_DECISIONS_TABLE} (
                effect_key TEXT PRIMARY KEY, tool_name TEXT NOT NULL, decision_identity TEXT NOT NULL,
                signed_decision_json TEXT NOT NULL, signed_outcome_json TEXT,
                outcome_delivered INTEGER NOT NULL DEFAULT 0)""")

    def __repr__(self) -> str:
        """Describe the dispatcher without its keys."""
        return f"ReceiptedEffects(budget_id={self.budget_id!r}, tools={sorted(self.bindings)!r})"

    def _connect(self) -> closing[sqlite3.Connection]:
        return closing(sqlite3.connect(self._path, timeout=30, isolation_level=None))

    async def execute(
        self,
        context: MiddlewareContext[mcp_types.CallToolRequestParams],
        call_next: CallNext[mcp_types.CallToolRequestParams, ToolResult],
        decision: GovernanceDecision,
        *,
        constitution: Constitution,
        reviewed: bool,
    ) -> ToolResult:
        """Run one call that ACGS has already allowed or a reviewer has approved.

        Bound tools are resolved through FastMCP, their arguments validated and coerced as FastMCP would for the
        tool body, and sent through their provider; the tool body never runs. Unbound tools run through
        ``call_next`` only if they are read-only (or ``refuse_unbound_side_effects`` is false).

        Args:
            context: The middleware context of the call.
            call_next: The rest of the middleware chain.
            decision: The ACGS decision that allowed (``ALLOW``) or, after approval, reviewed (``REVIEW``) the call.
            constitution: The constitution that made the decision.
            reviewed: Whether a reviewer approved the call.

        Returns:
            For a bound tool, a result whose structured content holds the effect key, the outcome and both receipts.

        Raises:
            ToolError: If the call is refused, the effect did not happen, its outcome is unknown or still in
                flight, or its outcome receipt is not available yet. Each message says whether the effect may have
                happened and, once it is reserved, names its key.
        """
        tool_name = context.message.name
        binding = self.bindings.get(tool_name)
        if binding is None and not self.refuse_unbound_side_effects:
            return await call_next(context)
        tool = await _resolve(context, tool_name)
        if binding is None:
            annotations = None if tool is None else tool.annotations
            if annotations is None or annotations.read_only_hint is not True:
                raise _refused(tool_name, _NO_SIDE_EFFECT_CONTRACT)
            return await call_next(context)
        if tool is None:
            raise _refused(tool_name, _UNRESOLVED)
        raw = dict(context.message.arguments or {})
        declared = _declares_key(tool)
        idempotency_key = _idempotency_key(context, tool_name, raw, declared=declared)
        arguments = _validated_arguments(tool, raw, strict=_strict(context))
        sent = _sent_parameters(tool, raw)
        if arguments is None or sent is None or not sent <= arguments.keys():
            raise _refused(tool_name, _BAD_ARGUMENTS)
        if declared:
            arguments.pop(IDEMPOTENCY_ARGUMENT, None)
            sent.discard(IDEMPOTENCY_ARGUMENT)
        try:
            # The coerced values of the arguments the caller sent (not the defaults), keyed by parameter name, so
            # "3" and 3 are one call while a retry after a tool default changed is still the same grant and
            # conflicts rather than being sent again.
            call_digest = _call_digest(tool_name, {name: arguments[name] for name in sorted(sent)})
            subject = _subject()
            grant = grant_identity(
                tool_name,
                call_digest,
                idempotency_key,
                subject=subject,
                provider_id=binding.provider.provider_id,
                budget_id=self.budget_id,
                amount=binding.amount,
            )
            self.store.registry.require_dispatchable(binding.provider.provider_id)
            signed_decision = self._sign_decision(
                decision, constitution, tool_name, call_digest, grant, subject, reviewed=reviewed
            )
        except Exception:
            logger.warning(
                "ACGS refused the contract or decision of tool '%s' (fail-closed).", tool_name, exc_info=True
            )
            raise _refused(tool_name, _CONTRACT_REFUSED) from None
        try:
            settled = await anyio.to_thread.run_sync(
                lambda: self._settle(tool_name, binding, grant, arguments, signed_decision)
            )
        except _Unsettled as unsettled:
            logger.warning(
                "ACGS effect %s for tool '%s' did not settle (state %s).",
                unsettled.key,
                tool_name,
                None if unsettled.state is None else unsettled.state.value,
                exc_info=True,
            )
            raise _unsettled(tool_name, unsettled) from None
        except _Conflict as conflict:
            logger.warning(
                "ACGS effect for tool '%s' conflicts with an effect already reserved under its grant.",
                tool_name,
                exc_info=True,
            )
            raise _conflicting(tool_name, conflict.key) from None
        except Exception:
            logger.warning(
                "ACGS effect for tool '%s' was refused before dispatch (fail-closed).", tool_name, exc_info=True
            )
            raise _refused(tool_name, _STORE_REFUSED) from None
        await anyio.to_thread.run_sync(self._deliver, settled)
        return _result(tool_name, settled)

    def _sign_decision(  # noqa: PLR0913
        self,
        decision: GovernanceDecision,
        constitution: Constitution,
        tool_name: str,
        call_digest: str,
        grant: str,
        subject: str | None,
        *,
        reviewed: bool,
    ) -> SignedReceipt:
        from acgs_lite.legitimacy import (  # noqa: PLC0415
            BASELINE_CONSTRAINT_MARKER,
            RECEIPT_SCHEMA_V2,
            DecisionReceipt,
            ExecutionBoundary,
            bind_policy,
            sign_receipt,
        )

        constraints = (*decision.rule_ids, *decision.warning_rule_ids) or (BASELINE_CONSTRAINT_MARKER,)
        receipt = DecisionReceipt.create(
            request_id=grant,
            goal=f"tools/call {tool_name}",
            proposed_method=tool_name,
            decision_type="ALLOW_WITH_CONTROLS" if reviewed else "ALLOW",
            authority_basis=f"acgs-constitution:{constitution.name}",
            matched_constraints=constraints,
            policy_version=constitution.hash_versioned,
            required_controls=(_HUMAN_REVIEW,) if reviewed else (),
            denial_or_review_rationale=decision.reason if reviewed else None,
            execution_boundary=ExecutionBoundary(
                allowed_method=tool_name,
                allowed_scope=f"call-digest:sha256:{call_digest}",
                allowed_subjects=() if subject is None else (subject,),
                expires_at=None,
                single_use=True,
            ),
            schema_version=RECEIPT_SCHEMA_V2,
            grant_id=grant,
            policy_digest=bind_policy(constitution).digest,
            method_id=tool_name,
            argument_digest=call_digest,
        )
        return sign_receipt(receipt, self.signer)

    def _settle(
        self,
        tool_name: str,
        binding: EffectBinding,
        grant: str,
        arguments: dict[str, Any],
        signed_decision: SignedReceipt,
    ) -> _Settled:
        """Reserve, record the decision, send once, and sign a terminal outcome. Blocking.

        An error from ``reserve`` propagates only when no effect is reserved under the grant: nothing happened. If
        one is (``reserve`` refuses a grant whose validated action changed, for example after a tool default
        changed), or the lookup fails, it raises :class:`_Conflict`, because that effect may already have happened.
        After ``reserve``, every failure and every non-terminal state raises :class:`_Unsettled` with the effect key
        and the state the store holds, so the client is never told that an effect which may have happened was not
        executed.
        """
        from acgs_lite.legitimacy import EffectState, signed_envelope_identity  # noqa: PLC0415

        try:
            record = self.store.store.reserve(
                provider_id=binding.provider.provider_id,
                grant_identity=grant,
                action={"tool": tool_name, "arguments": arguments},
                budget_id=self.budget_id,
                amount=binding.amount,
            )
        except Exception as exc:
            try:
                existing = self._reserved_key(grant)
            except Exception:
                logger.warning("ACGS could not look up the effect of grant %s.", grant, exc_info=True)
                raise _Conflict(None) from exc
            if existing is None:
                raise
            raise _Conflict(existing) from exc
        key = record.key
        try:
            with self._connect() as db:
                db.execute(
                    f"INSERT OR IGNORE INTO {_DECISIONS_TABLE} "  # noqa: S608
                    "(effect_key, tool_name, decision_identity, signed_decision_json) VALUES (?,?,?,?)",
                    (
                        key,
                        tool_name,
                        signed_envelope_identity(signed_decision),
                        json.dumps(signed_decision.to_dict(), sort_keys=True),
                    ),
                )
            record = self.store.send(key, binding.provider)
            if record.state in (EffectState.DONE, EffectState.NOT_DONE):
                return self._outcome(record, binding.provider)
        except Exception as exc:
            raise _Unsettled(key, self._state(key)) from exc
        raise _Unsettled(key, record.state)

    def _reserved_key(self, grant: str) -> str | None:
        """The key of the effect already reserved under ``grant``, or None. Blocking.

        Reads the ``EffectStore``'s own table, which has no public lookup by grant.
        """
        with self._connect() as db:
            row = db.execute("SELECT key FROM external_effects WHERE grant_identity=?", (grant,)).fetchone()
        return None if row is None else str(row[0])

    def _state(self, key: str) -> EffectState | None:
        try:
            return self.store.store.get(key).state
        except Exception:
            logger.warning("ACGS could not read the state of effect %s.", key, exc_info=True)
            return None

    def _outcome(self, record: EffectRecord, provider: EffectProvider) -> _Settled:
        """Sign the outcome of a terminal record once; later calls return the stored receipt. Blocking."""
        from acgs_lite.legitimacy import (  # noqa: PLC0415
            EffectOutcome,
            OutcomeReceipt,
            SignedOutcomeReceipt,
            sign_outcome,
        )

        with self._connect() as db:
            row = db.execute(
                f"SELECT decision_identity, signed_decision_json, signed_outcome_json, outcome_delivered "  # noqa: S608
                f"FROM {_DECISIONS_TABLE} WHERE effect_key=?",
                (record.key,),
            ).fetchone()
            if row is None:
                msg = f"no decision is recorded for effect {record.key}"
                raise ValueError(msg)
            decision_identity, decision_json, outcome_json, delivered = row
            if outcome_json is None:
                contract = self.store.contract_for(record.key, provider)
                outcome = sign_outcome(
                    OutcomeReceipt(
                        effect_key=record.key,
                        provider_id=record.request.provider_id,
                        provider_contract_digest=contract.digest,
                        decision_receipt_identity=decision_identity,
                        grant_identity=record.request.grant_identity,
                        outcome=EffectOutcome(record.state.value),
                        recorded_at=datetime.now(timezone.utc).isoformat(),
                    ),
                    self.signer,
                )
                db.execute(
                    f"UPDATE {_DECISIONS_TABLE} SET signed_outcome_json=? "  # noqa: S608
                    "WHERE effect_key=? AND signed_outcome_json IS NULL",
                    (json.dumps(outcome.to_dict(), sort_keys=True), record.key),
                )
                outcome_json, delivered = db.execute(
                    f"SELECT signed_outcome_json, outcome_delivered FROM {_DECISIONS_TABLE} "  # noqa: S608
                    "WHERE effect_key=?",
                    (record.key,),
                ).fetchone()
        return _Settled(
            record=record,
            decision_identity=decision_identity,
            signed_decision=json.loads(decision_json),
            outcome=SignedOutcomeReceipt.from_dict(json.loads(outcome_json)),
            delivered=bool(delivered),
        )

    def _deliver(self, settled: _Settled) -> None:
        """Pass an undelivered outcome receipt to ``on_outcome``, then mark it delivered. Blocking; never raises."""
        if settled.delivered or self.on_outcome is None:
            return
        try:
            self.on_outcome(settled.outcome)
            with self._connect() as db:
                db.execute(
                    f"UPDATE {_DECISIONS_TABLE} SET outcome_delivered=1 WHERE effect_key=?",  # noqa: S608
                    (settled.record.key,),
                )
        except Exception:
            logger.exception(
                "ACGS could not deliver the outcome receipt of effect %s; the next retry or reconcile delivers it.",
                settled.record.key,
            )

    def reconcile(self, effect_key: str, tool_name: str) -> SignedOutcomeReceipt | None:
        """Resolve an effect whose outcome is unknown through its provider's atomic query-and-fence.

        Blocking. Works after a restart, once the provider's contract is registered again. For an effect that is
        already DONE or NOT_DONE, it signs the outcome receipt if none is stored yet and delivers it if it has not
        been delivered. Do not reconcile an IN_FLIGHT effect while its send may still be running: the fence can turn
        an effect that would have been DONE into NOT_DONE. Reconcile IN_FLIGHT only after the sending process died.

        Args:
            effect_key: The key from the ``outcome unknown`` error.
            tool_name: The bound tool whose provider performed the effect.

        Returns:
            The signed outcome receipt if the effect is now DONE or NOT_DONE (passed to ``on_outcome`` unless it was
            already delivered), or None if it is still unknown.

        Raises:
            KeyError: If ``tool_name`` has no binding.
            ProviderNotDispatchable: If the provider has no dispatchable contract.
            ValueError: If the effect key is unknown or no decision is recorded for it.
        """
        from acgs_lite.legitimacy import EffectState  # noqa: PLC0415

        provider = self.bindings[tool_name].provider
        record = self.store.reconcile(effect_key, provider)
        if record.state not in (EffectState.DONE, EffectState.NOT_DONE):
            return None
        settled = self._outcome(record, provider)
        self._deliver(settled)
        return settled.outcome


class _Unsettled(Exception):  # noqa: N818
    """A reserved effect that did not settle with a receipt: its key and the store's state (None if unreadable)."""

    def __init__(self, key: str, state: EffectState | None) -> None:
        super().__init__(key)
        self.key = key
        self.state = state


class _Conflict(Exception):  # noqa: N818
    """``reserve`` refused a grant under which an effect may already be reserved: its key, or None if unreadable."""

    def __init__(self, key: str | None) -> None:
        super().__init__(key)
        self.key = key


async def _resolve(context: MiddlewareContext[mcp_types.CallToolRequestParams], tool_name: str) -> Tool | None:
    """The tool FastMCP would run for this caller: None if it is unknown, disabled, unauthorized or unresolvable."""
    ctx = context.fastmcp_context
    if ctx is None:
        return None
    try:
        return await ctx.fastmcp.get_tool(tool_name)
    except Exception:
        logger.warning("ACGS could not resolve tool '%s'; denying it.", tool_name, exc_info=True)
        return None


def _strict(context: MiddlewareContext[mcp_types.CallToolRequestParams]) -> bool:
    ctx = context.fastmcp_context
    return ctx is not None and getattr(ctx.fastmcp, "strict_input_validation", False) is True


def _validated_arguments(tool: Tool, arguments: dict[str, Any], *, strict: bool) -> dict[str, Any] | None:
    """The arguments as the tool would receive them, in JSON form, or None if they are invalid.

    A ``FunctionTool`` validates and coerces them with its own pydantic argument model, as FastMCP does before it
    runs the body (formats, validators, constrained types and defaults apply). Any other tool validates them against
    its input schema with format checks. None also when validation itself fails (fail-closed).
    """
    try:
        if isinstance(tool, FunctionTool):
            validated: Any = _function_arguments(tool, arguments, strict=strict)
        else:
            validator = validator_for(tool.parameters)
            validator.check_schema(tool.parameters)
            if not validator(tool.parameters, format_checker=validator.FORMAT_CHECKER).is_valid(arguments):
                return None
            validated = arguments
        jsonable = _ordered_sets(validated, to_jsonable_python(validated))
    except ValidationError:
        return None
    except Exception:
        logger.warning("ACGS could not validate the arguments of tool '%s'; denying it.", tool.name, exc_info=True)
        return None
    return jsonable if isinstance(jsonable, dict) else None


def _ordered_sets(value: object, serialized: object) -> object:
    """``serialized``, pydantic's JSON form of ``value``, with every list that came from a set in canonical order.

    pydantic serializes a set in iteration order, which for strings depends on the per-process hash seed, so the
    same call would get a different digest (and grant) on another worker or after a restart. Custom serializers
    keep their output; a set whose serialized form cannot be located raises ValueError (fail-closed).
    """
    if isinstance(value, Enum):
        return _ordered_sets(value.value, serialized)
    if isinstance(value, set | frozenset) and isinstance(serialized, list) and len(serialized) == len(value):
        # pydantic iterated this same object, so items and their serialized forms line up.
        ordered = (_ordered_sets(item, out) for item, out in zip(value, serialized, strict=True))
        return sorted(ordered, key=to_json)
    names = _field_names(value)
    if names is not None and isinstance(serialized, dict):
        return _ordered_fields(value, names, serialized)
    if (
        isinstance(value, Mapping)
        and isinstance(serialized, dict)
        and len(serialized) == len(value)
        and not any(map(_has_set, value))
    ):
        return {key: _ordered_sets(item, serialized[key]) for key, item in zip(serialized, value.values(), strict=True)}
    if isinstance(value, list | tuple) and isinstance(serialized, list) and len(serialized) == len(value):
        return [_ordered_sets(item, out) for item, out in zip(value, serialized, strict=True)]
    if _has_set(value):
        raise ValueError(_UNLOCATED_SET)
    return serialized


def _ordered_fields(value: object, names: list[str], serialized: dict[str, Any]) -> dict[str, Any]:
    result = dict(serialized)
    for name in names:
        if name in result:
            result[name] = _ordered_sets(getattr(value, name), result[name])
        elif _has_set(getattr(value, name)):
            raise ValueError(_UNLOCATED_SET)
    return result


_UNLOCATED_SET = "a set argument was serialized in a form whose order cannot be made canonical"


def _has_set(value: object) -> bool:
    return isinstance(value, set | frozenset) or any(map(_has_set, _parts(value)))


def _field_names(value: object) -> list[str] | None:
    """The field names of a pydantic model or dataclass instance, or None for any other value."""
    if isinstance(value, BaseModel):
        return list(type(value).model_fields)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [field.name for field in dataclasses.fields(value)]
    return None


def _parts(value: object) -> list[object]:
    """The values nested directly in ``value``."""
    if isinstance(value, Enum):
        return [value.value]
    names = _field_names(value)
    if names is not None:
        return [getattr(value, name) for name in names]
    if isinstance(value, Mapping):
        return [*value.keys(), *value.values()]
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _sent_parameters(tool: Tool, arguments: dict[str, Any]) -> set[str] | None:
    """The parameter names of the arguments the caller sent, or None if one does not name a parameter.

    A ``FunctionTool`` parameter may be sent under its pydantic alias; the validated arguments are keyed by
    parameter name. A key that maps to no parameter, or a parameter with an alias other than a plain string, is
    refused (fail-closed) rather than dropped from the digest.
    """
    if not isinstance(tool, FunctionTool):
        return set(arguments)
    try:
        signature, annotations = _parameters(without_injected_parameters(tool.fn))
    except Exception:
        logger.warning("ACGS could not read the parameters of tool '%s'; denying it.", tool.name, exc_info=True)
        return None
    names = {name: name for name in signature.parameters}
    for name, param in signature.parameters.items():
        fields = [item for item in get_args(annotations.get(name))[1:] if isinstance(item, FieldInfo)]
        if isinstance(param.default, FieldInfo):
            fields.append(param.default)
        for field in fields:
            alias = field.validation_alias if field.validation_alias is not None else field.alias
            if alias is None:
                continue
            # An alias that is another parameter's name or alias makes the mapping depend on parameter order, and
            # an aliased key argument would not be recognised as the idempotency key.
            if not isinstance(alias, str) or names.get(alias, name) != name or name == IDEMPOTENCY_ARGUMENT:
                return None
            names[alias] = name
    if not all(key in names for key in arguments):
        return None
    return {names[key] for key in arguments}


def _parameters(fn: Callable[..., object]) -> tuple[inspect.Signature, dict[str, Any]]:
    signature = inspect.signature(fn)
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    annotations = {
        name: hints.get(name, param.annotation)
        for name, param in signature.parameters.items()
        if name in hints or param.annotation is not inspect.Parameter.empty
    }
    return signature, annotations


def _function_arguments(tool: FunctionTool, arguments: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    """Validate with the pydantic model FastMCP builds from the tool function, without calling the function."""
    fn = without_injected_parameters(tool.fn)
    signature, annotations = _parameters(fn)

    def bind(*args: object, **kwargs: object) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)

    bind_fn = cast("Any", bind)
    bind_fn.__signature__ = signature.replace(return_annotation=inspect.Signature.empty)
    bind_fn.__annotations__ = annotations
    bind_fn.__module__ = getattr(fn, "__module__", bind.__module__)
    adapter: TypeAdapter[dict[str, Any]] = TypeAdapter(bind_fn)
    if strict:
        return adapter.validate_json(to_json(arguments), strict=True)
    return adapter.validate_python(arguments)


def _subject() -> str | None:
    """The authenticated caller as a canonical string (OAuth client, issuer and subject), or None if unauthenticated.

    The same components as ``mcp.server.auth.provider.principal_components``: a subject is unique only per issuer.
    """
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    sub = token.subject if token.subject is not None else claims.get("sub")
    iss = claims.get("iss")
    return json.dumps(
        [token.client_id, None if iss is None else str(iss), sub if isinstance(sub, str) else None],
        separators=(",", ":"),
    )


def _declares_key(tool: Tool) -> bool:
    """Whether the tool's input schema declares the ``idempotency_key`` argument."""
    properties = tool.parameters.get("properties") if isinstance(tool.parameters, Mapping) else None
    return isinstance(properties, Mapping) and IDEMPOTENCY_ARGUMENT in properties


def _idempotency_key(
    context: MiddlewareContext[mcp_types.CallToolRequestParams],
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    declared: bool,
) -> str:
    """The key from ``_meta`` or the declared argument. Raises ``ToolError`` if absent, malformed, or both differ."""
    ctx = context.fastmcp_context
    request = None if ctx is None else ctx.request_context
    meta = None if request is None else request.meta
    from_meta = meta.get(IDEMPOTENCY_KEY_META) if isinstance(meta, Mapping) else None
    from_argument = arguments.get(IDEMPOTENCY_ARGUMENT) if declared else None
    if from_meta is not None and from_argument is not None and from_meta != from_argument:
        raise _refused(tool_name, _KEY_MISMATCH)
    value = from_meta if from_meta is not None else from_argument
    if not isinstance(value, str) or _KEY_PATTERN.fullmatch(value) is None:
        raise _refused(tool_name, _NO_KEY)
    return value


def _refused(tool_name: str, reason: str) -> ToolError:
    return ToolError(f"Tool call '{tool_name}' was not executed. {reason}")


def _held(tool_name: str, key: str) -> ToolError:
    return ToolError(
        f"Tool call '{tool_name}': outcome unknown; held; reconcile with key {key}. It was not retried automatically."
    )


def _conflicting(tool_name: str, key: str | None) -> ToolError:
    if key is None:
        return ToolError(
            f"Tool call '{tool_name}': an effect may already be reserved under this idempotency key, but its key "
            "could not be read; it may already have happened and was not sent again. Retry with the same key."
        )
    return ToolError(
        f"Tool call '{tool_name}': effect {key} is already reserved under this idempotency key, but the tool now "
        "resolves this call to a different action; it may already have happened and was not sent again. "
        "Reconcile it before retrying with a new key."
    )


def _unsettled(tool_name: str, unsettled: _Unsettled) -> ToolError:
    from acgs_lite.legitimacy import EffectState  # noqa: PLC0415

    key, state = unsettled.key, unsettled.state
    same_key = "Retry with the same idempotency key; a new key would send the effect again."
    if state is EffectState.RESERVED:
        return _refused(tool_name, _STORE_REFUSED)
    if state is EffectState.UNKNOWN:
        return _held(tool_name, key)
    if state is EffectState.IN_FLIGHT:
        return ToolError(
            f"Tool call '{tool_name}': effect {key} is in flight. Retry later with the same idempotency key. "
            "Do not reconcile it while its send may still be running."
        )
    if state is None:
        return ToolError(f"Tool call '{tool_name}': the state of effect {key} could not be read. {same_key}")
    return ToolError(
        f"Tool call '{tool_name}': effect {key} is {state.name}; its outcome receipt is not available yet. "
        f"{same_key} Or reconcile it."
    )


def _result(tool_name: str, settled: _Settled) -> ToolResult:
    from acgs_lite.legitimacy import EffectState  # noqa: PLC0415

    outcome = settled.outcome
    if settled.record.state is EffectState.NOT_DONE:
        msg = (
            f"Tool call '{tool_name}': the effect did not happen (NOT_DONE, fenced by the provider). "
            f"Outcome receipt: {outcome.receipt.identity}."
        )
        raise ToolError(msg)
    structured = {
        "effect_key": settled.record.key,
        "outcome": "DONE",
        "provider_reference": outcome.receipt.provider_reference,
        "outcome_receipt": outcome.to_dict(),
        "decision_receipt_identity": settled.decision_identity,
        "decision_receipt": settled.signed_decision,
    }
    return ToolResult(content=f"Effect {settled.record.key} is DONE.", structured_content=structured)


__all__ = [
    "GRANT_DOMAIN",
    "IDEMPOTENCY_ARGUMENT",
    "IDEMPOTENCY_KEY_META",
    "EffectBinding",
    "ReceiptedEffects",
    "grant_identity",
]
