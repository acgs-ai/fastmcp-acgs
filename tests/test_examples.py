# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The README only shows code that runs, and the quickstart output it shows is the real output."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from fastmcp_acgs import ACGSMiddleware
from tests.fakes import review_rules

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
QUICKSTART = ROOT / "examples" / "quickstart.py"


def _blocks(language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)]


def test_quickstart_output_matches_readme() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", str(QUICKSTART)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        timeout=300,
    )
    [expected] = [block for block in _blocks("text") if block.startswith("send_email:")]
    assert completed.stdout == expected
    assert completed.stderr == ""


def test_readme_embeds_the_quickstart_verbatim() -> None:
    assert QUICKSTART.read_text(encoding="utf-8") in _blocks("python")


def test_deny_and_audit_runs_offline_without_api_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in list(os.environ):
        if key.endswith("API_KEY"):
            monkeypatch.delenv(key)
    # Register the example's import-time environment change so pytest restores its original value.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    # pytest-socket blocks network access during this in-process execution.
    runpy.run_path(str(ROOT / "examples" / "deny_and_audit.py"), run_name="__main__")
    output = capsys.readouterr().out
    execution, audit = output.split("Audit trail (application decisions):\n")
    assert execution.splitlines() == [
        "Executed record_note: The meeting starts at noon.",
        "Denied record_note: Tool call 'record_note' was not executed. "
        "Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).",
    ]
    decisions = [json.loads(line) for line in audit.splitlines()]
    assert [decision["verdict"] for decision in decisions] == ["allow", "deny"]
    assert [decision["tool_name"] for decision in decisions] == ["record_note", "record_note"]
    assert decisions[0]["rule_ids"] == []
    assert decisions[1]["rule_ids"] == ["ACGS-006"]
    assert "Blocked by" in decisions[1]["reason"]
    assert all(decision["error"] is None for decision in decisions)
    assert "demo-secret" not in output


def test_readme_states_the_supported_versions() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package, pinned in (("fastmcp-slim[server]", "fastmcp-slim[server,client]"), ("acgs-lite", "acgs-lite")):
        [requirement] = re.findall(rf'"({re.escape(package)}>=[^"]+)"', pyproject)
        assert f"`{requirement}`" in README
        [floor] = re.findall(rf'"{re.escape(pinned)}==([^"]+)"', pyproject)
        assert requirement.startswith(f"{package}>={floor},")
    assert README.count("fastmcp-slim 4.1.0") == 2


def _middleware(mcp: FastMCP[Any]) -> ACGSMiddleware:
    [guard] = [m for m in mcp.middleware if isinstance(m, ACGSMiddleware)]
    return guard


def test_every_readme_python_block_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = _blocks("python")
    assert len(blocks) == 5
    namespaces: list[dict[str, Any]] = []
    for block in blocks:
        namespace: dict[str, Any] = {"__name__": "readme", "constitution": review_rules()}
        exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
        namespaces.append(namespace)
    assert "normalized: update config | dry run" in capsys.readouterr().out
    assert _middleware(namespaces[0]["mcp"]).on_review == "deny"
    assert _middleware(namespaces[3]["mcp"]).on_review == "elicit"
    assert _middleware(namespaces[-1]["mcp"]).agent_id == "payments-server"
