# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.3.0 (unreleased)

### Added

- Experimental receipted effects (`fastmcp_acgs.effects`): `ReceiptedEffects` and `EffectBinding`, passed as
  `ACGSMiddleware(effects=...)`. Allowed or approved calls to bound tools are sent once through a FENCEABLE
  provider contract instead of running the tool body, keyed by a client idempotency key in `_meta`
  (`acgs/idempotency-key`) or a declared `idempotency_key` tool argument (`IDEMPOTENCY_ARGUMENT`), and return a
  signed `OutcomeReceipt` chained to a signed v2 decision receipt. Unknown outcomes are held for
  `ReceiptedEffects.reconcile`; BOUNDED/UNBOUNDED providers and unbound tools without `readOnlyHint` are denied.
  Bound tools are resolved through FastMCP first (disabled, unregistered and auth-refused tools are denied), their
  arguments are validated against the tool's input schema, and grant identities (`fastmcp-acgs:grant:v2:...`) are
  scoped to the authenticated caller. Failures after dispatch report the effect's real state and key instead of
  "not executed", and outcome receipts reach `on_outcome` at least once.

### Changed

- Require `acgs-lite>=4.0.0,<5` (and declare `jsonschema>=4.20.0,<5`) so receipted effects and `OutcomeReceipt`
  verification run natively without skips or coverage exclusions.
- Idempotency keys (from `_meta` or the argument) must be 16 to 128 characters from `A-Z a-z 0-9 _ -`.
- The call digest behind a receipted effect's key covers coerced validated caller arguments keyed by parameter
  name (including pydantic aliases) with canonical set ordering.

## 0.2.0 (2026-11-20)

### Added

- Offline `examples/deny_and_audit.py`: one allowed and one denied FastMCP tool call, with an application decision trail.
- Compatibility tests for nested JSON-native arguments and bytes/bytearray formatting against acgs-lite 4.0.0.

### Changed

- Document the compatibility matrix: acgs-lite 4.0.0 is tested in the locked environment, and the CI `test-min`
  job tests the 3.0.1 floor. Runtime support remains `acgs-lite>=3.0.1,<5`.
- Clarify that the adapter uses text validation and does not issue signed execution receipts.

## 0.1.0 (unreleased)

### Added

- `ACGSMiddleware`: a FastMCP middleware (`on_call_tool`) that checks each tool call's name and arguments
  against an ACGS constitution before the tool runs, and raises `ToolError` with the reason when it denies it.
- Review handling: `on_review="deny"` (default), or `on_review="elicit"`, which asks the client's user to approve
  the exact call through MCP elicitation on both protocol eras (`ctx.elicit()` on handshake connections, an
  `InputRequiredResult` round on 2026-07-28 connections). Declined, unsupported, failed and timed-out reviews deny
  the call.
- `review_timeout=` and `tool_call_formatter=` options; `ACGSMiddleware.evaluate`.
