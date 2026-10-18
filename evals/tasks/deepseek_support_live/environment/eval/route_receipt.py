"""Record completed real LiteLLM routes, without prompts, headers, or credentials.

Loaded by the installed LiteLLM proxy as a success callback. A caller-supplied
header or a command mentioning LiteLLM never creates a receipt.
"""
from __future__ import annotations

import asyncio
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger


class RouteReceipt(CustomLogger):
    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        response = response_obj.model_dump()
        params = kwargs.get("litellm_params") or {}
        choices = response.get("choices") or []
        receipt = {
            "schema_version": 1,
            "source": "litellm.proxy.success_callback",
            "litellm_version": version("litellm"),
            "litellm_path": str(Path(litellm.__file__).resolve()),
            "call_type": kwargs.get("call_type"),
            "provider": params.get("custom_llm_provider"),
            "model": kwargs.get("model"),
            "api_base": params.get("api_base"),
            "response_id": response.get("id"),
            "response_model": response.get("model"),
            "content": (choices[0].get("message") or {}).get("content") if choices else None,
            "finish_reason": choices[0].get("finish_reason") if choices else None,
            "completed_at": end_time.isoformat(),
        }
        path = Path(os.environ["LITELLM_ROUTE_RECEIPTS"])
        await asyncio.to_thread(self._append, path, receipt)

    @staticmethod
    def _append(path: Path, receipt: dict[str, Any]) -> None:
        with path.open("a") as output:
            output.write(json.dumps(receipt) + "\n")


receipt_logger = RouteReceipt()
