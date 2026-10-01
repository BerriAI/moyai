"""Publish a durable receipt before independently finishing its file archive."""
import asyncio
from pathlib import Path

FAST_RECEIPT_LIMIT = 1024 * 1024
_volume_write_lock = asyncio.Lock()


class ReceiptStorage:
    def __init__(self, root: Path, put, commit):
        self.root, self.put, self.commit = root, put, commit

    async def save(self, name, value):
        # Small receipts survive process loss as soon as put acknowledges. Large
        # results must wait for the Volume to keep Dict entries safely bounded.
        if len(value) <= FAST_RECEIPT_LIMIT:
            await self.put(name, value)
        if name.endswith('.headers'):
            return
        # Each job owns unique filenames. Serialize writes/commits within this
        # container; the atomic remote claim fences writers across containers.
        async with _volume_write_lock:
            path = self.root / name
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_bytes(value)
            temporary.replace(path)
            await self.commit()
