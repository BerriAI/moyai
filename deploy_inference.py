"""Deploy only the trusted inference service; never the legacy Modal web app.

See docs/durable-inference.md for secret provisioning and staged rollout.
"""
import os
from pathlib import Path

import modal

app = modal.App('moyai-inference')
volume = modal.Volume.from_name('moyai-inference-results-v1', create_if_missing=True)
ledger = modal.Dict.from_name('moyai-inference-ledger-v1', create_if_missing=True)
image = (modal.Image.debian_slim(python_version='3.13')
         .pip_install('httpx>=0.28,<1', 'cryptography>=44')
         .add_local_python_source('inference'))


@app.function(image=image, secrets=[modal.Secret.from_name('moyai-inference-v1')],
              volumes={'/receipts': volume}, timeout=600, retries=0,
              cpu=0.25, memory=1024, min_containers=0, max_containers=25,
              scaledown_window=60, include_source=False)
@modal.concurrent(max_inputs=4)
async def complete(envelope: bytes):
    from cryptography.fernet import Fernet
    from inference.worker import execute
    from inference.storage import ReceiptStorage

    try:
        await execute(envelope, key=os.environ['LITELLM_API_KEY'], base=os.environ['LITELLM_API_BASE'],
                      cipher=Fernet(os.environ['INFERENCE_ENCRYPTION_KEY'].encode()),
                      storage=ReceiptStorage(Path('/receipts'), ledger.put.aio, volume.commit.aio),
                      claim=lambda job_id: ledger.put.aio(job_id, True, skip_if_exists=True))
    except Exception:
        # Modal captures exception diagnostics: do not expose HTTP bodies/keys.
        raise RuntimeError('Inference worker could not persist a result') from None
    return True  # Prompts and model output are never in control-plane results.
