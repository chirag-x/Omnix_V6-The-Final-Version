"""
Omnix V6 — System 3 (Vision) visual trace subpackage.

Public surface for the JSONL visual trace writer.  Off by
default; opt-in via the ``OMNIX_VISUAL_TRACE`` env var or
:func:`set_visual_trace_enabled`.
"""
from .visual_trace import (
    VisualTraceRecord,
    VisualTrace,
    is_visual_trace_enabled,
    set_visual_trace_enabled,
    get_visual_trace,
    trace_event,
    DEFAULT_TRACE_PATH,
)


__all__ = [
    "VisualTraceRecord",
    "VisualTrace",
    "is_visual_trace_enabled",
    "set_visual_trace_enabled",
    "get_visual_trace",
    "trace_event",
    "DEFAULT_TRACE_PATH",
]
