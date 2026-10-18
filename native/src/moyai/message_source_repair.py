"""Review/apply provenance-validated legacy transcript repair without changing execution inputs."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select

from .message_sources import public_message
from .models import Message, MessageSource


async def repair_page(database, *, apply: bool = False, after: int = 0, limit: int = 200) -> dict:
    async with database.sessions.begin() as db:
        if apply and database.product_authority_enabled:
            from .product_drain import ProductDrainService
            from .product_drain_models import ProductDrainState
            state = await db.get(ProductDrainState, 1, with_for_update=True)
            if state is None or not state.paused or await ProductDrainService._live_workers(db):
                raise RuntimeError("Repair requires a paused maintenance window with all native workers retired.")
        rows = list(await db.scalars(select(Message).where(
            Message.id > after, Message.role == "user", Message.author_id.startswith("slack:"),
            ~select(MessageSource.message_identity).where(MessageSource.message_identity == Message.identity).exists(),
        ).order_by(Message.id).limit(limit)))
        report = []
        for row in rows:
            content, source = await public_message(database, db, row)
            action = "review" if source and source.get("legacy_review_required") else "repair" if source else "unchanged"
            report.append({"message_id": row.id, "session_id": row.session_id, "action": action})
            if apply and source:
                # Keep a sealed recovery copy, including for old resume rows
                # whose Turn.prompt is empty. It is never part of source_view.
                source["legacy_original"] = database.security.unseal(row.content).decode()
                row.content = database.security.seal(content)
                db.add(MessageSource(message_identity=row.identity, kind="slack", mirror=False,
                                     data=database.security.seal(source)))
        return {"applied": apply, "messages": report, "next_after": rows[-1].id if len(rows) == limit else None}


async def run(apply: bool):
    from .product_config import load_product_config
    from .runtime import Runtime
    from .settings import Settings

    config = load_product_config()
    if config is None or config.product_mode != "existing":
        raise RuntimeError("Configure the existing product environment before reviewing or applying repairs.")
    runtime = Runtime(Settings(), product_config=config)
    try:
        # Verification only in product mode: provision the versioned schema
        # using the normal release path before running this command.
        await runtime.database.start()
        after = 0
        while True:
            result = await repair_page(runtime.database, apply=apply, after=after)
            print(json.dumps(result))
            after = result["next_after"]
            if after is None:
                break
    finally:
        await runtime.database.close()
        if runtime.product is not None:
            await runtime.product.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply reviewed repairs; execution prompts remain frozen.")
    args = parser.parse_args()
    asyncio.run(run(args.apply))
