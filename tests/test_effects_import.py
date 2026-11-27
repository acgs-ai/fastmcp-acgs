# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""fastmcp_acgs works without the acgs-lite provider-contract API, and says how to get it."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from acgs_lite import legitimacy

import fastmcp_acgs
from fastmcp_acgs.effects import EffectBinding, ReceiptedEffects


def test_importing_the_package_does_not_import_effects_eagerly() -> None:
    assert fastmcp_acgs.ACGSMiddleware is not None
    assert "ReceiptedEffects" not in fastmcp_acgs.__all__


def test_without_the_provider_contract_api_construction_raises_an_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(legitimacy, "ProviderContract", raising=False)
    store: Any = object()
    with pytest.raises(ImportError, match="provider-contract API"):
        ReceiptedEffects(store, store, budget_id="b", bindings={})
    assert "fastmcp_acgs.effects" in sys.modules


def test_effect_binding_needs_no_provider_contract_api() -> None:
    provider: Any = object()
    assert EffectBinding(provider).amount == 1
