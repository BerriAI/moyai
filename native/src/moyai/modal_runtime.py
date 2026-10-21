"""Select Modal's runtime once per application process, before any allocation."""

from __future__ import annotations

import os

from .settings import Settings


def configure_modal_runtime(settings: Settings) -> None:
    # Modal 1.5.5 reads this setting for every create, including Harness lazy
    # allocation, product preparation, and filesystem restoration. Keep that
    # SDK pinned: Harness 0.55's error mapper is incompatible with Modal 1.6.
    # This is deployment-wide, never a temporary override around an async call.
    configured = os.environ.get("MODAL_FUNCTION_RUNTIME")
    if configured is not None and configured != settings.modal_runtime:
        raise ValueError(
            "MOYAI_MODAL_RUNTIME conflicts with MODAL_FUNCTION_RUNTIME or an already "
            "configured runtime. Use one Modal runtime per process and restart to change it."
        )
    os.environ["MODAL_FUNCTION_RUNTIME"] = settings.modal_runtime
