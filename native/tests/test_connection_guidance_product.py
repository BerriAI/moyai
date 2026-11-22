"""Canonical individual identity checks on disposable PostgreSQL."""

import os
from dataclasses import replace

import pytest
from fastapi import HTTPException
from test_product_authority import active_turn
from test_product_authority import admitted_product as admitted_product  # noqa: PLC0414

from moyai.connection_guidance import ConnectionGuide

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Use disposable local PostgreSQL.")


async def test_individual_catalog_and_nonindividual_denial(admitted_product, monkeypatch):
    product = admitted_product
    _, _, prepared = await active_turn(product, private=True)
    scope = prepared.scope
    guide = ConnectionGuide(product.database)
    result = await guide.read(scope, scope.root_run_id)
    assert result.status == "empty"

    original = product.adapter.authorize_admission

    async def shared_identity(db, grant):
        current = await original(db, grant)
        return replace(current, kind="shared", method="password")

    monkeypatch.setattr(product.adapter, "authorize_admission", shared_identity)
    try:
        result = await guide.read(scope, scope.root_run_id)
    except (HTTPException, PermissionError):
        return
    assert result.status == "private_chat_required" and not result.connections
