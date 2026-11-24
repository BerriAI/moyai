"""Personal GitHub grants use the real canonical Slack/work identity mapping."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import test_product_identity_admission as identities
from fastapi import HTTPException
from sqlalchemy import text, update

from moyai.models import Connection, User
from moyai.oauth_models import OAuthCredential
from moyai.personal_oauth import connection_id

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Requires local PostgreSQL")
product_database = identities.product_database
admitted_product = identities.admitted_product
slack_admission = identities.slack_admission


@pytest.mark.parametrize("change", ["stale", "unverified", "conflict", "ambiguous", "owner_disabled", "manual"])
async def test_personal_github_fences_current_slack_relationship(slack_admission, change):
    product = slack_admission
    identity = connection_id(product.user_id, "github")
    async with product.database.sessions.begin() as db:
        db.add(Connection(
            id=identity, owner_id=product.user_id, kind="github", name="Personal GitHub",
            credential=product.database.security.seal(""), shared=False, enabled=True,
            policy=json.dumps({"oauth": True}),
        ))
        await db.flush()
        db.add(OAuthCredential(connection_id=identity, encrypted="synthetic-encrypted-grant"))
    async with product.database.sessions() as db:
        grants = await product.database.native_grants(
            db, product.raw_principal.id, [identity], shared_session=True, surface="slack",
        )
        assert len(grants) == 1
    async with product.database.sessions.begin() as db:
        if change == "owner_disabled":
            await db.execute(update(User).where(User.id == product.user_id).values(enabled=False))
        elif change == "ambiguous":
            await db.execute(text(
                "INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at) "
                "VALUES ('google:ambiguous','google','admin@example.test','Ambiguous','c','u')"
            ))
        elif change == "stale":
            await db.execute(text("UPDATE moyai.users SET profile_checked_at=:stamp WHERE id=:id"), {
                "stamp": (datetime.now(UTC) - timedelta(hours=2)).isoformat(), "id": product.raw_principal.id,
            })
        else:
            # Even a retained manual/accounting link cannot override email or
            # profile evidence for credential access.
            clause = {
                "unverified": "profile_eligible=0", "conflict": "profile_conflict=1",
                "manual": "email='someone-else@example.test'",
            }[change]
            await db.execute(text(f"UPDATE moyai.users SET {clause} WHERE id=:id"), {
                "id": product.raw_principal.id,
            })
    async with product.database.sessions() as db:
        with pytest.raises(HTTPException):
            await product.database.native_grants(
                db, product.raw_principal.id, [identity], shared_session=True, surface="slack",
            )
