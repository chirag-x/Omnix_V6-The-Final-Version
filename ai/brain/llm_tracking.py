"""
Omnix V6 — System 2 Brain LLM call tracker (Phase 17).

The tracker is a thin wrapper around an LLM invocation.  It:

  * records the start/end time of each call,
  * captures the provider / model / token counts when available,
  * exposes a single :meth:`record_call` that returns an
    :class:`LLMCallRecord`,
  * is safe to use from any thread.

The tracker is **pure data + a function**.  It never imports a
provider, a Windows service, or a capability.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .task.models import LLMCallRecord, TaskFactory, now


@dataclass
class LLMCallTracker:
    """A small helper that builds :class:`LLMCallRecord` values.

    The tracker is constructed once at Brain boot.  Callers (the
    interpreter, the LLM planner, the recovery engine) call
    :meth:`record_call` with the start time and the result.
    """

    prefix: str = "llm"
    metadata: Dict[str, Any] = field(default_factory=dict)
    _factory: TaskFactory = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_factory", TaskFactory(prefix=self.prefix))

    def record_call(
        self,
        *,
        reason: str,
        step_id: str = "",
        started_at: float,
        ended_at: Optional[float] = None,
        succeeded: bool = True,
        error_code: str = "",
        provider: str = "",
        model: str = "",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LLMCallRecord:
        end = ended_at if ended_at is not None else now()
        merged_meta = dict(self.metadata)
        if metadata:
            merged_meta.update(metadata)
        return self._factory.new_llm_call(
            reason=reason,
            step_id=step_id,
            started_at=started_at,
            ended_at=end,
            succeeded=bool(succeeded),
            error_code=error_code or "",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metadata=merged_meta,
        )


__all__ = ["LLMCallTracker"]
