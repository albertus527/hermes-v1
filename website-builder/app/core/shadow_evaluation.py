"""Compatibility shim for the shadow evaluation entry point.

The preview gate and the request dataclass now live in
``app.core.shadow_runner``, which is the single module that owns "run a
shadow evaluation". This module re-exports them so the existing imports
keep working, and adds nothing of its own.
"""

from app.core.shadow_runner import (
    ShadowEvaluationRequest,
    ShadowNotReady,
    evaluate_after_preview,
    preview_already_delivered,
)

__all__ = [
    "ShadowEvaluationRequest",
    "ShadowNotReady",
    "evaluate_after_preview",
    "preview_already_delivered",
]
