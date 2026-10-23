# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.1.0 (unreleased)

### Added

- `ACGSMiddleware`: a FastMCP middleware (`on_call_tool`) that checks each tool call's name and arguments
  against an ACGS constitution before the tool runs, and raises `ToolError` with the reason when it denies it.
- Review handling: `on_review="deny"` (default), or `on_review="elicit"`, which asks the client's user to approve
  the exact call through MCP elicitation on both protocol eras (`ctx.elicit()` on handshake connections, an
  `InputRequiredResult` round on 2026-07-28 connections). Declined, unsupported, failed and timed-out reviews deny
  the call.
- `review_timeout=` and `tool_call_formatter=` options; `ACGSMiddleware.evaluate`.
