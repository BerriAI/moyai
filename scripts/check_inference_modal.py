"""Cloud storage/latency smoke test using a fake gateway and ephemeral storage.

Run from the repository root: .venv/bin/python scripts/check_inference_modal.py
Uses only local Modal credentials; never passes the real gateway key and
never contacts an LLM. The temporary Modal app/Dict/Volume end with this script.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import modal
from cryptography.fernet import Fernet
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inference.worker import decode, encode

app = modal.App('moyai-inference-storage-check')
image = (modal.Image.debian_slim(python_version='3.13')
         .pip_install('httpx>=0.28,<1', 'cryptography>=44')
         .add_local_python_source('inference'))


async def main():
    configuration = dotenv_values('.env')
    client = await modal.Client.from_credentials.aio(configuration['MODAL_TOKEN_ID'], configuration['MODAL_TOKEN_SECRET'])
    cipher_key = Fernet.generate_key().decode()
    cipher = Fernet(cipher_key.encode())
    async with modal.Volume.ephemeral(client=client) as volume, modal.Dict.ephemeral(client=client) as claims:
        @app.function(image=image, volumes={'/receipts': volume},
                      secrets=[modal.Secret.from_dict({'TEST_RECEIPT_KEY': cipher_key})],
                      cpu=0.25, memory=1024, timeout=120, retries=0, max_containers=1, include_source=False, serialized=True)
        async def complete(envelope):
            import httpx
            from inference.worker import execute
            from inference.storage import ReceiptStorage

            timings = {'commits': []}
            class Storage(ReceiptStorage):
                async def save(self, name, content):
                    start = time.perf_counter()
                    await super().save(name, content)
                    if not name.endswith('.headers'):
                        timings['commits'].append(round(time.perf_counter()-start, 4))

            async def gateway(request):
                timings['gateway_calls'] = timings.get('gateway_calls', 0) + 1
                return httpx.Response(200, headers={'x-litellm-response-cost':'0.125'},
                                      json={'choices':[{'message':{'content':'Synthetic response; no LLM called'}}],
                                            'usage':{'total_tokens':3}})
            start = time.perf_counter()
            await execute(envelope, key='synthetic-only', base='https://synthetic.invalid/v1',
                          cipher=Fernet(os.environ['TEST_RECEIPT_KEY'].encode()),
                          storage=Storage(Path('/receipts'), claims.put.aio, volume.commit.aio),
                          claim=lambda job_id: claims.put.aio(job_id, True, skip_if_exists=True),
                          client_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(gateway), **kw))
            timings['worker_seconds'] = round(time.perf_counter()-start, 4)
            return timings

        async with app.run(client=client):
            results = []
            first_envelope = None
            for index in range(4):
                job_id = uuid4().hex
                envelope = encode(cipher, {'id':job_id, 'created':time.time(),
                    'key_hash':hashlib.sha256(b'synthetic-only').hexdigest(),
                    'base':'https://synthetic.invalid/v1', 'payload':{'messages':[], 'stream':False}})
                first_envelope = first_envelope or envelope
                start = time.perf_counter()
                call = await complete.spawn.aio(envelope)
                while not await claims.contains.aio(job_id + '.result'):
                    await asyncio.sleep(0.25)
                raw = await claims.get.aio(job_id + '.result')
                received = time.perf_counter()
                result = decode(cipher, raw)
                assert result['status'] == 'completed' and result['cost'] == '0.125'
                metrics = await call.get.aio()
                archived = bytearray()
                async for chunk in volume.read_file.aio(job_id + '.result'):
                    archived.extend(chunk)
                assert bytes(archived) == raw
                metrics.update(attempt=index+1, response_ready_seconds=round(received-start,4),
                               archive_verified_seconds=round(time.perf_counter()-start,4))
                results.append(metrics)
                print(json.dumps(metrics), flush=True)
            duplicate = await complete.remote.aio(first_envelope)
            assert duplicate.get('gateway_calls', 0) == 0
            print(json.dumps({'duplicate_gateway_calls': 0, 'synthetic_only': True}), flush=True)


if __name__ == '__main__':
    with modal.enable_output():
        asyncio.run(main())
