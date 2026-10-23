# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""A governed server run over stdio by ``test_stdio.py``."""

from __future__ import annotations

from fastmcp_acgs import ACGSMiddleware
from tests.fakes import Ran, server

if __name__ == "__main__":
    ran = Ran()
    mcp = server(ran, middleware=[ACGSMiddleware(on_review="elicit")])

    @mcp.tool
    def skip_audit(job: str) -> str:
        """A tool whose name needs review under the default constitution."""
        return f"skipped {job}"

    mcp.run(transport="stdio", show_banner=False)
