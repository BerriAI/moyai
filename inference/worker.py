"""One external attempt per job. Receipts survive the Render request/process.

The atomic claim expires in Modal after seven inactive days. A signed job is
eligible to submit for only one hour, so expiry can never authorize a replay.
"""
import asyncio
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation

import httpx
from cryptography.fernet import Fernet

MAX_RESPONSE = 8 * 1024 * 1024
MAX_ENVELOPE = 16 * 1024 * 1024
SUBMIT_WINDOW = 3600
RECOVER_WINDOW = 30 * 86400


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def encode(cipher, value):
    return cipher.encrypt(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode())


def decode(cipher, value):
    if len(value) > MAX_ENVELOPE:
        raise ValueError('Inference envelope too large')
    return json.loads(cipher.decrypt(value))


async def execute(envelope, *, key, base, cipher, storage, claim, client_factory=httpx.AsyncClient):
    """Dependencies are explicit so crash boundaries can be fault-injected.

    storage.save must commit to durable storage before returning. Never log an
    exception body, payload, response or credential from this function.
    """
    job = decode(cipher, envelope)
    job_id = job['id']
    if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('Invalid inference ID')
    if time.time() - job['created'] > SUBMIT_WINDOW or job['created'] > time.time() + 60:
        return  # Retrieval is separate; expired envelopes never submit again.
    if not await claim(job_id):
        return
    receipt = {'id': job_id, 'key_hash': job['key_hash'], 'status': 'unknown',
               'cost': None, 'cost_source': '', 'gateway_id': '', 'usage': {}, 'finished': None}

    async def save(suffix):
        encrypted = encode(cipher, receipt)
        # Retry storage, never inference. Keep the answer in memory while the
        # durable volume has a transient failure.
        for attempt in range(5):
            try:
                await storage.save(job_id + suffix, encrypted)
                return
            except Exception:
                if attempt == 4:
                    raise RuntimeError('Inference receipt storage unavailable') from None
                await asyncio.sleep(2 ** attempt)

    if (hashlib.sha256(key.encode()).hexdigest() != job['key_hash']
            or base.rstrip('/') != job['base'].rstrip('/') or not key
            or time.time() - job['created'] > SUBMIT_WINDOW):
        receipt.update(status='rejected', cost='0', cost_source='not_submitted', finished=time.time())
        await save('.result')
        return
    # The remote atomic claim is itself a durable start marker. A filesystem
    # commit here would add latency without strengthening duplicate protection.
    try:
        async with asyncio.timeout(330):
            async with client_factory(timeout=httpx.Timeout(300, connect=30), follow_redirects=False) as client:
                async with client.stream('POST', base.rstrip('/') + '/chat/completions', json=job['payload'],
                                         headers={'Authorization': f'Bearer {key}', 'x-litellm-call-id': job_id}) as response:
                    receipt.update(cost=money(response.headers.get('x-litellm-response-cost')),
                                   gateway_id=response.headers.get('x-litellm-call-id', '')[:200])
                    if receipt['cost'] is not None:
                        receipt['cost_source'] = 'response_header'
                    await save('.headers')
                    if response.status_code >= 400:
                        receipt['status'] = 'failed'
                    else:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > MAX_RESPONSE:
                                raise ValueError('Response too large')
                        value = json.loads(raw)
                        if not isinstance(value, dict) or 'error' in value or not isinstance(value.get('choices'), list):
                            raise ValueError('Invalid completion')
                        # Parse cost separately to preserve decimal precision.
                        precise = json.loads(raw, parse_float=Decimal)
                        usage = precise.get('usage') or {}
                        receipt['usage'] = {k: v for k, v in usage.items()
                                            if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                            and type(v) is int and v >= 0} if isinstance(usage, dict) else {}
                        if receipt['cost'] is None:
                            receipt['cost'] = money(precise.get('x_litellm_response_cost'))
                            if receipt['cost'] is None and isinstance(usage, dict):
                                receipt['cost'] = money(usage.get('x_litellm_response_cost'))
                            if receipt['cost'] is not None:
                                receipt['cost_source'] = 'response_usage'
                        receipt.update(status='completed', response=value)
    except Exception:
        # The gateway may already have billed. Preserve any received cost and
        # make the uncertainty explicit; no HTTP or activity retry calls it again.
        receipt['status'] = 'unknown'
    receipt['finished'] = time.time()
    await save('.result')
