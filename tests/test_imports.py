# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
import fastmcp_acgs


def test_public_api() -> None:
    assert sorted(fastmcp_acgs.__all__) == sorted(
        [
            "REVIEW_INPUT_KEY",
            "ACGSMiddleware",
            "GovernanceDecision",
            "OnReview",
            "ToolCallFormatter",
            "Verdict",
            "format_tool_call",
        ]
    )
    for name in fastmcp_acgs.__all__:
        assert getattr(fastmcp_acgs, name) is not None
