"""
Omnix V6 — Default verifier implementations (Phase 6C + Phase 7.1).

Tri-state semantics
-------------------
A :class:`VerificationVerdict` is always one of:

  * ``PASSED``     — observation matches the expected effect.
  * ``FAILED``     — observation contradicts the expected effect.
  * ``UNCERTAIN``  — observation is missing, ambiguous, or the
                     capability did not actually verify (the
                     "EXECUTED ≠ VERIFIED" invariant).

Phase 7.1 addition
------------------
``DefaultStepVerifier`` now consumes a *before* observation as well
as the *after* observation.  The before/after pair is the only way
to know that a *state change* actually happened — the prior Phase
6 verifier only saw a single observation, which led to false
``verified=True`` results (the step ran, the world did not change).

The before/after diff is purely structural (R-22: vision is
deterministic, no LLM in the verification loop).  The verifier
classifies the diff against the :class:`ExpectedEffect` in a
closed, documented way:

  * ``check_name == "vision_observed"``: PASSED iff the diff says
    "target appeared" or "target changed in a way consistent with
    the expected effect".
  * ``check_name == "vision_disappeared"``: PASSED iff the diff
    says "target disappeared".
  * any other check_name: falls through to the Phase 6C logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import (
    ExecutionContext,
    ExpectedEffect,
    Goal,
    Observation,
    VerificationVerdict,
)


# ===========================================================================
# Tri-state verdict helpers
# ===========================================================================

_VERDICT_PASSED = "passed"
_VERDICT_FAILED = "failed"
_VERDICT_UNCERTAIN = "uncertain"

# Phase 14.2: ``VerificationStatus`` (in :mod:`core.results`) uses
# ``VERIFIED / MISMATCH / UNVERIFIED`` as the canonical enum value
# names.  The verifier above was originally written against the
# shorter ``passed / failed / uncertain`` triple — capabilities that
# emit :class:`VerificationResult` with ``status=VerificationStatus.VERIFIED``
# therefore miss the verdict-passed branch entirely and fall
# through to ``UNCERTAIN``.  Treat the canonical enum strings as
# aliases so both shapes are honoured without forcing every
# capability to translate its own enum.
_VERDICT_PASSED_ALIASES = ("verified",)
_VERDICT_FAILED_ALIASES = ("mismatch", "timed_out")
_VERDICT_UNCERTAIN_ALIASES = ("unverified",)


def _verdict(
    *,
    check_name: str,
    expected: Any,
    actual: Any,
    passed: bool,
    failed: bool,
    uncertain: bool,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    confidence: float = 1.0,
) -> VerificationVerdict:
    return VerificationVerdict(
        passed=passed,
        failed=failed,
        uncertain=uncertain,
        check_name=check_name,
        expected=expected,
        actual=actual,
        reason=reason,
        metadata=dict(metadata or {}),
        confidence=confidence,
    )


def passed_verdict(
    *,
    check_name: str,
    expected: Any = None,
    actual: Any = None,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    confidence: float = 1.0,
) -> VerificationVerdict:
    """Build a :data:`PASSED` verdict.

    ``confidence`` defaults to 1.0 (strict).  Callers that have only
    a partial signal (e.g. an observation that "looks right" but
    lacks explicit verification) should pass a lower value
    (typically 0.7) to signal that the verdict is correct but the
    underlying evidence is incomplete.
    """
    return _verdict(
        check_name=check_name,
        expected=expected,
        actual=actual,
        passed=True,
        failed=False,
        uncertain=False,
        reason=reason or "observation matches expected effect",
        metadata=metadata,
        confidence=confidence,
    )


def failed_verdict(
    *,
    check_name: str,
    expected: Any = None,
    actual: Any = None,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    confidence: float = 0.0,
) -> VerificationVerdict:
    """Build a :data:`FAILED` verdict.

    ``confidence`` defaults to 0.0 because a failed verdict with
    zero supporting evidence is a *hard* failure: the recovery
    engine should treat it as terminal.  When the failure is
    ambiguous (e.g. timeout before completion) the caller can
    pass 0.3 to indicate the failure is "likely but not certain".
    """
    return _verdict(
        check_name=check_name,
        expected=expected,
        actual=actual,
        passed=False,
        failed=True,
        uncertain=False,
        reason=reason or "observation contradicts expected effect",
        metadata=metadata,
        confidence=confidence,
    )


def uncertain_verdict(
    *,
    check_name: str,
    expected: Any = None,
    actual: Any = None,
    reason: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    confidence: float = 0.3,
) -> VerificationVerdict:
    """Build an :data:`UNCERTAIN` verdict.

    ``confidence`` defaults to 0.3 because uncertainty is the
    weakest signal — we have some evidence but cannot reach a
    binary conclusion.  A 0.3 verdict should be re-checked
    (vision diff, ground-truth probe) before the recovery
    engine acts on it.
    """
    return _verdict(
        check_name=check_name,
        expected=expected,
        actual=actual,
        passed=False,
        failed=False,
        uncertain=True,
        reason=reason or "observation missing or ambiguous",
        metadata=metadata,
        confidence=confidence,
    )


# ===========================================================================
# Default step verifier
# ===========================================================================

class DefaultStepVerifier:
    """Deterministic step-level :class:`Verifier`.

    Compares the :class:`ExpectedEffect` declared on the step
    against the post-action :class:`Observation` (and optionally
    a *before* observation for state-change checks).

    The default comparison rules are deliberately conservative:

      * If the observation is ``None``, the verdict is
        :data:`UNCERTAIN`.
      * If the observation's data is missing the ``verification``
        block, the verdict is :data:`UNCERTAIN`.
      * If the observation's ``status`` is one of
        ``"failed"``, ``"timed_out"``, ``"cancelled"``,
        ``"blocked"``, ``"skipped"`` the verdict is
        :data:`FAILED`.
      * If the observation's data carries an explicit verification
        status (``"passed"``/``"failed"``/``"uncertain"``) we
        honour it.
      * Otherwise (a clean ``succeeded`` step with no explicit
        verification block), the verdict is :data:`UNCERTAIN`.
        This is the R-8 "EXECUTED ≠ VERIFIED" boundary.

    Phase 7.1 addition: when the :class:`ExpectedEffect` carries
    a ``before_observation`` in its metadata, the verifier
    classifies the before/after diff instead of the after-only
    observation.  This is how vision-based effects are now
    verified — never as a single-screenshot "verified=True".
    """

    name: str = "default-step"

    def verify(
        self,
        *,
        effect: ExpectedEffect,
        observation: Optional[Observation],
        before_observation: Optional[Observation] = None,
        context: Optional[ExecutionContext] = None,
    ) -> VerificationVerdict:
        check_name = effect.check_name or "unknown_check"

        # Phase 7.1: vision-style effects go through the diff path.
        if check_name in ("vision_observed", "vision_disappeared", "vision_changed"):
            return self._verify_vision_effect(
                effect=effect,
                observation=observation,
                before_observation=before_observation,
                check_name=check_name,
            )

        if observation is None:
            return uncertain_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=None,
                reason="no observation available",
                confidence=0.1,
            )

        data = observation.data if isinstance(observation.data, dict) else {}
        step_status = data.get("status", "")
        cap_status = data.get("capability_status", "")
        verification = data.get("verification") if isinstance(data, dict) else None

        # Hard negatives first: the step did not complete at all.
        if step_status in ("failed", "timed_out", "cancelled"):
            return failed_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=data.get("details"),
                reason=f"step status is {step_status!r}",
                confidence=1.0,
            )

        if step_status in ("blocked", "skipped"):
            return failed_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=data.get("details"),
                reason=f"step did not run (status={step_status!r})",
                confidence=1.0,
            )

        # The capability's own verification block (if any) wins.
        if isinstance(verification, dict):
            v_status = (verification.get("status") or "").lower()
            v_actual = verification.get("actual")
            v_expected = verification.get("expected")
            if v_status == _VERDICT_PASSED or v_status in _VERDICT_PASSED_ALIASES:
                return passed_verdict(
                    check_name=check_name,
                    expected=v_expected if v_expected is not None else effect.expected,
                    actual=v_actual,
                    reason="capability reported verification passed",
                    confidence=1.0,
                )
            if v_status == _VERDICT_FAILED or v_status in _VERDICT_FAILED_ALIASES:
                return failed_verdict(
                    check_name=check_name,
                    expected=v_expected if v_expected is not None else effect.expected,
                    actual=v_actual,
                    reason="capability reported verification failed",
                    confidence=1.0,
                )
            if v_status == _VERDICT_UNCERTAIN or v_status in _VERDICT_UNCERTAIN_ALIASES:
                return uncertain_verdict(
                    check_name=check_name,
                    expected=v_expected if v_expected is not None else effect.expected,
                    actual=v_actual,
                    reason="capability reported verification uncertain",
                    confidence=0.5,
                )

        # Conservative default: a clean succeeded step without an
        # explicit verification is UNCERTAIN, not PASSED.  This
        # carries 0.5 confidence so the recovery engine still has
        # *some* signal — a 0.3 would say "we know nothing", but
        # here we know the step ran.
        if step_status == "succeeded" and cap_status not in ("verified",):
            return uncertain_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=data.get("details"),
                reason=(
                    "step succeeded but capability did not report explicit "
                    "verification (EXECUTED ≠ VERIFIED)"
                ),
                confidence=0.5,
            )

        if cap_status == "verified" or step_status == "succeeded":
            if observation.confidence >= 1.0:
                return passed_verdict(
                    check_name=check_name,
                    expected=effect.expected,
                    actual=data.get("details"),
                    reason="capability reported VERIFIED",
                    confidence=1.0,
                )

        return uncertain_verdict(
            check_name=check_name,
            expected=effect.expected,
            actual=data.get("details"),
            reason="no positive verification signal",
            confidence=0.3,
        )

    # ----------------------------------------------------- Phase 7.1
    def _verify_vision_effect(
        self,
        *,
        effect: ExpectedEffect,
        observation: Optional[Observation],
        before_observation: Optional[Observation],
        check_name: str,
    ) -> VerificationVerdict:
        """Classify a before/after diff against a vision effect.

        The check is structural — no LLM, no heuristics.  The
        caller must put a diff dict in ``observation.data`` with
        a ``changed`` boolean (and a ``reason`` for transparency).
        """
        if observation is None:
            return uncertain_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=None,
                reason="no after-observation available",
                confidence=0.1,
            )

        data = observation.data if isinstance(observation.data, dict) else {}
        changed = data.get("changed")
        reason = data.get("reason", "")

        # The diff is itself a closed enum.  We refuse to infer
        # anything beyond what it says.
        if changed is True:
            if check_name == "vision_disappeared":
                if reason == "target disappeared":
                    return passed_verdict(
                        check_name=check_name,
                        expected=effect.expected,
                        actual=data,
                        reason="target disappeared as expected",
                        confidence=1.0,
                    )
                return failed_verdict(
                    check_name=check_name,
                    expected=effect.expected,
                    actual=data,
                    reason=(
                        f"expected target to disappear but the diff is "
                        f"{reason!r}"
                    ),
                    confidence=0.8,
                )
            # Default for vision_observed / vision_changed: any
            # detected change is enough to PASS the structural
            # check; the Brain will read the diff details.
            return passed_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=data,
                reason=f"vision diff reports {reason or 'change'}",
                confidence=0.7,
            )

        if changed is False:
            return failed_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=data,
                reason=(
                    f"vision diff reports no change "
                    f"(reason={reason!r})"
                ),
                confidence=1.0,
            )

        # changed is None — typically because one of the two
        # observations was missing.  This is the conservative
        # "we don't know" path.
        return uncertain_verdict(
            check_name=check_name,
            expected=effect.expected,
            actual=data,
            reason=(
                f"vision diff is uncertain (reason={reason!r}; "
                f"before_observation={'present' if before_observation else 'missing'})"
            ),
            confidence=0.2,
        )


# ===========================================================================
# Default goal verifier
# ===========================================================================

class DefaultGoalVerifier:
    """Deterministic goal-level :class:`Verifier`."""

    name: str = "default-goal"

    def verify(
        self,
        *,
        effect: ExpectedEffect,
        observation: Optional[Observation],
        context: Optional[ExecutionContext] = None,
    ) -> VerificationVerdict:
        check_name = effect.check_name or "goal"

        if observation is None:
            return uncertain_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual=None,
                reason="no aggregate observation available",
            )

        data = observation.data if isinstance(observation.data, dict) else {}
        verdicts_raw = data.get("step_verdicts", []) or []
        passed_count = 0
        failed_count = 0
        uncertain_count = 0
        for v in verdicts_raw:
            if not isinstance(v, dict):
                continue
            status = (v.get("status") or "").lower()
            if status == _VERDICT_PASSED or status in _VERDICT_PASSED_ALIASES:
                passed_count += 1
            elif status == _VERDICT_FAILED or status in _VERDICT_FAILED_ALIASES:
                failed_count += 1
            elif status == _VERDICT_UNCERTAIN or status in _VERDICT_UNCERTAIN_ALIASES:
                uncertain_count += 1

        total = passed_count + failed_count + uncertain_count
        metadata = {
            "passed": passed_count,
            "failed": failed_count,
            "uncertain": uncertain_count,
            "total": total,
        }

        if failed_count > 0:
            return failed_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual={"passed": passed_count, "failed": failed_count,
                        "uncertain": uncertain_count},
                reason=f"{failed_count} of {total} step verifications failed",
                metadata=metadata,
            )
        if total == 0 or uncertain_count > 0:
            return uncertain_verdict(
                check_name=check_name,
                expected=effect.expected,
                actual={"passed": passed_count, "failed": failed_count,
                        "uncertain": uncertain_count},
                reason=(
                    f"{uncertain_count} of {total} step verifications "
                    "uncertain (and none failed)"
                ),
                metadata=metadata,
            )
        return passed_verdict(
            check_name=check_name,
            expected=effect.expected,
            actual={"passed": passed_count, "failed": failed_count,
                    "uncertain": uncertain_count},
            reason=f"all {total} step verifications passed",
            metadata=metadata,
        )


__all__ = [
    "DefaultStepVerifier",
    "DefaultGoalVerifier",
    "passed_verdict",
    "failed_verdict",
    "uncertain_verdict",
]
