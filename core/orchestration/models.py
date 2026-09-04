"""
Omnix V6 — Orchestration domain models (Phase 4).

Frozen dataclasses that flow between the IntentInterpreter, the Planner,
the PlanExecutor, the Verifier, and the RecoveryEngine.  No business
logic lives here — the models are inert data.

Each model:
    - is ``frozen=True`` (R-10: immutability);
    - has ``to_dict`` for structured logging and audit;
    - exposes ``with_*`` methods for snapshot updates.

R-21 enforcement lives on :class:`ActionRequest`: a free-form shell
command is rejected at construction time.  The closed capability set
is the only valid action surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, List, Tuple, runtime_checkable


# ===========================================================================
# Goal
# ===========================================================================

@dataclass(frozen=True)
class Goal:
    """A user-facing objective the agent is trying to achieve.

    ``Goal`` is the *contract* with the user.  It is what the planner
    decomposes and what the verifier eventually checks.  It does NOT
    carry a command name or a capability reference (R-24): a goal is
    expressed in user terms; the plan decides which capabilities to
    invoke.
    """

    goal_id: str
    description: str
    success_criteria: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()
    priority: int = 0
    created_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Goal",
            "goal_id": self.goal_id,
            "description": self.description,
            "success_criteria": list(self.success_criteria),
            "constraints": list(self.constraints),
            "priority": self.priority,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Intent
# ===========================================================================

class IntentKind(str, Enum):
    """The kind of intent the interpreter has produced.

    Intents are *internal* structured representations (R-24).  They
    are never the surface a user types; the user types free-form
    utterances that get classified into one of these.

    Two layers exist:
      * **Dialogue kinds** (``inform``, ``query``, ``command``,
        ``clarify``, ``cancel``, ``unknown``) describe the *speech
        act* of the user utterance.
      * **Action kinds** (``open_application``,
        ``close_application``, ``control_application``,
        ``focus_application``, ``file_find``, ``file_move``,
        ``file_copy``, ``file_delete``, ``query_status``,
        ``cancel_task``) describe the *domain objective* the user
        wants the agent to achieve.

    The interpreter may populate **both** layers: a single user
    utterance such as "Open Chrome" is a ``COMMAND`` whose normalized
    objective is ``OPEN_APPLICATION``.  The dialogue kind is the
    speech act; the action kind is the structured objective.  This
    split lets the future Planner reason about *what the user wants
    to achieve* (``OPEN_APPLICATION``) without conflating that with
    *how the user phrased it* (``command``).
    """

    # ---- Dialogue layer (speech act) -----------------------------------
    INFORM = "inform"          # the user is conveying information
    QUERY = "query"            # the user is asking a question
    COMMAND = "command"        # the user wants the agent to do something
    CLARIFY = "clarify"        # the user is asking the agent to clarify
    CANCEL = "cancel"          # the user is cancelling a prior request
    UNKNOWN = "unknown"        # could not classify

    # ---- Action layer (domain objective) --------------------------------
    # The intent model is *semantic*, not capability-specific.  We do
    # not add kinds like ``spotify_play_music`` or
    # ``chrome_search_google`` — those would hard-code workflows.
    # When the capability set grows, new kinds are added here only if
    # the underlying objective is genuinely distinct from existing
    # kinds.
    OPEN_APPLICATION = "open_application"
    CLOSE_APPLICATION = "close_application"
    FOCUS_APPLICATION = "focus_application"
    CONTROL_APPLICATION = "control_application"   # generic app-level action
    FILE_FIND = "file_find"                        # search the filesystem
    FILE_MOVE = "file_move"                        # relocate files
    FILE_COPY = "file_copy"                        # duplicate files
    FILE_DELETE = "file_delete"                    # remove files
    WINDOW_MANAGE = "window_manage"                # generic window action
    GENERATE_CONTENT = "generate_content"          # request AI generation of text, code, or content
    QUERY_STATUS = "query_status"                  # ask the world a question
    CANCEL_TASK = "cancel_task"                    # halt an in-flight plan
    NO_OP = "no_op"                                # ack / no work needed
    COMPOUND_REQUEST = "compound_request"           # multi-action command (Phase 14.2)
    # ---- UI grounding intents (Phase 7.3) -----------------------------
    # The user wants the agent to *target* a screen element described
    # by a human-readable query.  These intents require a vision
    # grounding before dispatch — the planner must emit
    # ``vision_pre_action`` / ``vision_target_query`` metadata for
    # them, and the Agent must consult the vision service before any
    # click.
    UI_CLICK_TARGET = "ui_click_target"
    UI_DOUBLE_CLICK_TARGET = "ui_double_click_target"
    UI_RIGHT_CLICK_TARGET = "ui_right_click_target"
    # ---- Browser intents (Phase 8) -----------------------------------
    # The user wants the agent to perform an action *in a web browser*
    # rather than on the desktop.  These are not reducible to the
    # ``CONTROL_APPLICATION`` kind because the target is a DOM element
    # inside a page, not a native window.  They map to the closed
    # browser capability set (browser.navigate, browser.click,
    # browser.type, browser.extract_text) and are routed through the
    # canonical :class:`BrowserService`.  Like the UI_*_TARGET kinds,
    # ``BROWSER_CLICK_TARGET`` and ``BROWSER_TYPE_TARGET`` accept a
    # ``target_query`` that the planner can use to either (a) supply a
    # concrete locator to the capability or (b) ask the vision
    # fallback to ground it.
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK_TARGET = "browser_click_target"
    BROWSER_TYPE_TARGET = "browser_type_target"
    BROWSER_EXTRACT_TEXT = "browser_extract_text"


# ---------------------------------------------------------------------------
# Intent parameter typing — used by the IntentSpec layer (Phase 5B)
# ---------------------------------------------------------------------------

class IntentParamType(str, Enum):
    """The declared type of an :class:`Intent` parameter.

    This is *narrower* than :class:`core.capability.ParamType`:
    Intent parameters describe the user's objective, not a capability
    signature.  They never include ``PATH`` because application/file
    resolution belongs to the application/file capability layers,
    not to the interpreter.
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    ENUM = "enum"           # one of a closed set
    LIST_OF_STRINGS = "list_of_strings"
    ANY = "any"             # opaque; the interpreter does not check shape


@dataclass(frozen=True)
class Intent:
    """A structured interpretation of a user utterance (Phase 5B).

    The model is the *internal* contract (R-24) between the
    :class:`IntentInterpreter` and the future :class:`Planner`.  It
    describes the user's *objective* in a planner-friendly form,
    *not* the sequence of capabilities that will achieve it.

    Two layers of kind:

      * ``dialogue_kind`` is the speech act (e.g. ``COMMAND``,
        ``QUERY``, ``CANCEL``).  It describes *how* the user phrased
        the request.
      * ``kind`` is the semantic action (e.g. ``OPEN_APPLICATION``,
        ``FILE_FIND``).  It describes *what* the user wants the
        agent to achieve.  For a simple "Open Chrome", both are
        populated: ``kind=OPEN_APPLICATION``,
        ``dialogue_kind=COMMAND``.

    Parameters
    ----------
    parameters:
        Validated, structured parameters of the intent.  For
        ``OPEN_APPLICATION`` this is e.g.
        ``{"application": "chrome"}``.  Parameters are *semantic*
        (``application``, not ``exe_path``); application resolution
        is the responsibility of the application capability layer,
        not the interpreter.

    confidence:
        A number in [0.0, 1.0].  Callers (typically the orchestrator)
        decide a threshold below which they ask the user to confirm
        before proceeding.  Confidence is *metadata* — it must never
        bypass safety or validation.
    """

    intent_id: str
    kind: IntentKind
    text: str
    dialogue_kind: IntentKind = IntentKind.COMMAND
    parameters: Mapping[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    referenced_entities: Tuple[str, ...] = ()
    referenced_goal_id: Optional[str] = None
    source_text: str = ""             # the raw user text, preserved
    constraints: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    def with_confidence(self, confidence: float) -> "Intent":
        return replace(self, confidence=max(0.0, min(1.0, confidence)))

    def with_parameter(self, key: str, value: Any) -> "Intent":
        new_params = dict(self.parameters)
        new_params[key] = value
        return replace(self, parameters=new_params)

    def to_goal(
        self,
        *,
        goal_id: Optional[str] = None,
        success_criteria: Tuple[str, ...] = (),
        priority: int = 0,
    ) -> "Goal":
        """Convert this :class:`Intent` to a :class:`Goal`.

        The Goal retains:
          * the original user text (``description``),
          * the normalized objective (built from the semantic kind
            and parameters), in ``metadata["normalized_objective"]``,
          * the success criteria the caller wants to enforce.

        The conversion is a *projection*, not a copy: the Goal is a
        separate model with its own lifecycle (R-24).  The future
        Planner decomposes the Goal into PlanSteps; the Intent is
        the input, the Goal is the contract.
        """
        if goal_id is None:
            goal_id = f"goal_{self.intent_id}"
        normalized = self._normalized_objective()
        description = self.source_text or self.text
        # Merge the semantic description into the goal description so
        # the verifier can see both the raw utterance and the
        # normalized objective without re-deriving them.
        full_description = f"{description}"
        return Goal(
            goal_id=goal_id,
            description=full_description,
            success_criteria=success_criteria,
            constraints=tuple(self.constraints),
            priority=priority,
            metadata={
                "intent_id": self.intent_id,
                "intent_kind": self.kind.value,
                "dialogue_kind": self.dialogue_kind.value,
                "normalized_objective": normalized,
                "parameters": dict(self.parameters),
                "source_text": self.source_text,
            },
        )

    def _normalized_objective(self) -> str:
        """Return a short, planner-readable description of the intent.

        The format is ``"<kind> <k>=<v> <k>=<v>"``.  It is *not* an
        execution instruction; the Planner will decompose it.
        """
        parts: List[str] = [self.kind.value]
        for k, v in sorted(self.parameters.items()):
            parts.append(f"{k}={v}")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Intent",
            "intent_id": self.intent_id,
            "kind": self.kind.value,
            "dialogue_kind": self.dialogue_kind.value,
            "text": self.text,
            "source_text": self.source_text,
            "parameters": dict(self.parameters),
            "constraints": list(self.constraints),
            "confidence": self.confidence,
            "referenced_entities": list(self.referenced_entities),
            "referenced_goal_id": self.referenced_goal_id,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# ActionRequest
# ===========================================================================

class ActionKind(str, Enum):
    """The shape of the action a PlanStep is asking for.

    Note: this is the *kind of action in the plan*; the *name* comes
    from the closed :class:`core.capability.CapabilityRegistry` set.
    The PlanExecutor MUST resolve a PlanStep into an
    :class:`ActionRequest` whose ``capability_name`` is registered.
    """

    CAPABILITY_CALL = "capability_call"   # invoke a registered capability
    OBSERVE = "observe"                    # sense the world (no side effect)
    VERIFY = "verify"                      # run a verification check
    WAIT = "wait"                          # pause for a future event
    ASK_USER = "ask_user"                  # surface a question to the user


# R-21: explicit anti-shell patterns.  ``ActionRequest.__post_init__``
# rejects any of these in a capability name, parameter key, or
# parameter value.  This is the foundation's *static* defense against
# shell escape: even if a model was constructed with a hostile
# payload, the construction would have raised.
_FORBIDDEN_SHELL_TOKENS = (
    "&&", "||", ";", "|",
    "$(", "`",  # command substitution
    ">", "<",  # redirection
    "rm -rf", "del /f", "format ", "shutdown",  # destructive
)

_FORBIDDEN_SHELL_PATTERN = re.compile(
    "|".join(re.escape(tok) for tok in _FORBIDDEN_SHELL_TOKENS),
    re.IGNORECASE,
)


def _validate_no_shell_payload(name: str, value: Any) -> None:
    """Reject shell-like tokens in any string payload of an action.

    This is a *static* check at construction time.  It does not
    promise to catch every attack — that is the CapabilityRouter's
    job — but it raises an explicit error if a PlanStep or
    ActionRequest was constructed with a payload that smells like a
    shell escape.  That makes the failure loud and the audit trail
    honest.
    """
    if isinstance(value, str) and _FORBIDDEN_SHELL_PATTERN.search(value):
        raise ValueError(
            f"Action payload {name!r} contains a forbidden shell-like "
            f"token.  The orchestration layer cannot carry shell "
            f"commands; route through a registered capability "
            f"({value!r})."
        )
    if isinstance(value, (list, tuple)):
        for idx, item in enumerate(value):
            _validate_no_shell_payload(f"{name}[{idx}]", item)
    if isinstance(value, dict):
        for k, v in value.items():
            _validate_no_shell_payload(f"{name}.{k}", v)


@dataclass(frozen=True)
class ActionRequest:
    """A closed, validated request to invoke a registered capability.

    The capability name MUST resolve in the
    :class:`core.capability.CapabilityRegistry`.  Parameters are
    passed through as a dict; the router is responsible for coercion
    and validation against the spec.

    R-21 / Phase 4 hardening: this dataclass is the *only* path from
    the orchestration layer to the capability router.  It carries no
    shell, no eval, no raw path with shell metacharacters.  Any
    payload that smells like a shell escape is rejected at
    construction.

    Phase 6A enrichment: the PlanExecutor stamps ``plan_id``,
    ``step_id``, ``timeout_s``, ``safety_metadata`` and
    ``correlation_id`` so the router / services can attribute the
    action, honour the per-step deadline, and propagate safety
    classification through the boundary.  These fields are *additive*
    — Phase 4 / Phase 5 callers may construct an ``ActionRequest``
    without them and the executor backfills the values at dispatch.
    """

    capability_name: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    expected_effect: Optional["ExpectedEffect"] = None
    issued_at: Optional[float] = None
    # --- Phase 6A: execution-boundary enrichment -----------------------
    plan_id: str = ""
    step_id: str = ""
    timeout_s: Optional[float] = None
    safety_metadata: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.capability_name, str) or not self.capability_name.strip():
            raise ValueError(
                "ActionRequest.capability_name must be a non-empty string"
            )
        # Reject shell-like tokens anywhere in the payload.
        _validate_no_shell_payload("capability_name", self.capability_name)
        _validate_no_shell_payload("parameters", dict(self.parameters))
        _validate_no_shell_payload("safety_metadata", dict(self.safety_metadata))
        if self.expected_effect is not None:
            _validate_no_shell_payload("expected_effect", self.expected_effect.to_dict())
        # Defensive: timeout_s must be non-negative if provided.
        if self.timeout_s is not None and (
            not isinstance(self.timeout_s, (int, float))
            or self.timeout_s < 0.0
        ):
            raise ValueError(
                "ActionRequest.timeout_s must be a non-negative number"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ActionRequest",
            "request_id": self.request_id,
            "capability_name": self.capability_name,
            "parameters": dict(self.parameters),
            "expected_effect": (
                self.expected_effect.to_dict() if self.expected_effect else None
            ),
            "issued_at": self.issued_at,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "timeout_s": self.timeout_s,
            "safety_metadata": dict(self.safety_metadata),
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
        }

    # ----------------------------------------------------- with_* updates
    def with_timeout(self, timeout_s: float) -> "ActionRequest":
        """Return a copy of this request with ``timeout_s`` set."""
        return replace(self, timeout_s=timeout_s)

    def with_safety_metadata(self, **extra: Any) -> "ActionRequest":
        """Return a copy of this request with safety metadata merged in."""
        merged = dict(self.safety_metadata)
        merged.update(extra)
        return replace(self, safety_metadata=merged)

    def with_plan_step(
        self,
        plan_id: str,
        step_id: str,
        *,
        correlation_id: str = "",
    ) -> "ActionRequest":
        """Return a copy stamped with the originating plan/step ids."""
        return replace(
            self,
            plan_id=plan_id,
            step_id=step_id,
            correlation_id=correlation_id or self.correlation_id,
        )


# ===========================================================================
# ExpectedEffect
# ===========================================================================

@dataclass(frozen=True)
class ExpectedEffect:
    """A claim about what the world will look like after an action.

    The :class:`Verifier` compares an :class:`Observation` against
    the expected effect.  ``check_name`` is a free-form name the
    verifier knows how to dispatch on (``"app_is_running"``,
    ``"window_has_focus"``, etc.).
    """

    check_name: str
    expected: Any = None
    timeout_s: float = 0.0
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ExpectedEffect",
            "check_name": self.check_name,
            "expected": self.expected,
            "timeout_s": self.timeout_s,
            "description": self.description,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Observation
# ===========================================================================

class ObservationSource(str, Enum):
    """The sensor the observation came from."""

    SCREEN = "screen"
    UIA = "uia"
    DOM = "dom"
    OCR = "ocr"
    VISION = "vision"
    CLIPBOARD = "clipboard"
    PROCESS = "process"
    FILESYSTEM = "filesystem"
    WORLD = "world"          # the WorldState container
    USER = "user"            # user explicitly stated something
    DERIVED = "derived"      # computed from other observations


@dataclass(frozen=True)
class Observation:
    """A snapshot of something the agent sensed about the world.

    Observations are immutable.  The recovery layer compares a
    post-action observation against the :class:`ExpectedEffect` and
    against earlier observations to decide what changed.
    """

    source: ObservationSource
    data: Any = None
    timestamp: float = 0.0
    subject: str = ""
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Observation",
            "source": self.source.value,
            "data": self.data,
            "timestamp": self.timestamp,
            "subject": self.subject,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Plan + PlanStep
# ===========================================================================

class PlanStatus(str, Enum):
    """Lifecycle of a :class:`Plan`."""

    DRAFT = "draft"               # planner produced it; not started
    READY = "ready"               # accepted; ready to execute
    EXECUTING = "executing"       # one or more steps in progress
    PAUSED = "paused"             # executor paused (waiting on user / time)
    COMPLETED = "completed"       # all steps verified
    FAILED = "failed"             # unrecoverable failure
    CANCELLED = "cancelled"       # user cancelled
    REPLANNING = "replanning"     # planner is producing a new plan


@dataclass(frozen=True)
class PlanStep:
    """One step in a :class:`Plan`.

    A step is a *request to act or sense*, not the act itself.  The
    :class:`PlanExecutor` resolves the step into an
    :class:`ActionRequest` (or a verify / wait / ask_user flow) and
    dispatches it.  Steps have a stable ``step_id`` so replanning
    can preserve checkpoints.
    """

    step_id: str
    description: str
    action: ActionKind = ActionKind.CAPABILITY_CALL
    capability_name: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    expected_effect: Optional[ExpectedEffect] = None
    depends_on: Tuple[str, ...] = ()
    timeout_s: float = 30.0
    # Phase 1 / D6: ``max_retries`` is **deprecated**.  The
    # recovery engine is the single source of truth for retry
    # policy via :class:`RecoveryPolicy.max_attempts_per_step`.
    # Per-step override never reached the recovery engine
    # (audit finding D6).  We keep accepting the field for one
    # release cycle to give callers time to migrate, but emit a
    # :class:`DeprecationWarning` whenever a non-zero value is
    # passed.  In a future release this field will be removed.
    max_retries: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensive: shell-like payloads are forbidden even on
        # PlanStep, because the executor turns a PlanStep into an
        # ActionRequest and we want the same defense at the plan
        # boundary as at the action boundary.
        if self.action is ActionKind.CAPABILITY_CALL:
            if not isinstance(self.capability_name, str) or not self.capability_name.strip():
                raise ValueError(
                    "PlanStep with action=CAPABILITY_CALL must declare "
                    "a non-empty capability_name"
                )
            _validate_no_shell_payload(
                f"step[{self.step_id}].capability_name", self.capability_name
            )
            _validate_no_shell_payload(
                f"step[{self.step_id}].parameters", dict(self.parameters)
            )
            if self.expected_effect is not None:
                _validate_no_shell_payload(
                    f"step[{self.step_id}].expected_effect",
                    self.expected_effect.to_dict(),
                )
        # Phase 1 / D6: warn once when callers still pass a
        # non-zero ``max_retries``.  Use warnings.warn so
        # downstream tests can capture it via
        # ``pytest.warns(DeprecationWarning)``.
        if int(self.max_retries) > 0:
            import warnings
            warnings.warn(
                "PlanStep.max_retries is deprecated and will be "
                "removed in a future release.  Per-step retry "
                "policy is governed by RecoveryPolicy.max_attempts_per_step. "
                f"step_id={self.step_id!r} max_retries={self.max_retries!r}",
                DeprecationWarning,
                stacklevel=2,
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "PlanStep",
            "step_id": self.step_id,
            "description": self.description,
            "action": self.action.value,
            "capability_name": self.capability_name,
            "parameters": dict(self.parameters),
            "expected_effect": (
                self.expected_effect.to_dict() if self.expected_effect else None
            ),
            "depends_on": list(self.depends_on),
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Plan:
    """An ordered-or-DAG set of steps that achieve a :class:`Goal`.

    ``steps`` is stored as a tuple for immutability.  The
    :class:`Planner` interface is the only thing that should
    construct a Plan; the executor consumes it.
    """

    plan_id: str
    goal_id: str
    steps: Tuple[PlanStep, ...] = ()
    status: PlanStatus = PlanStatus.DRAFT
    created_at: Optional[float] = None
    replan_count: int = 0
    parent_plan_id: Optional[str] = None
    notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- derived
    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def step_ids(self) -> Tuple[str, ...]:
        return tuple(s.step_id for s in self.steps)

    def find_step(self, step_id: str) -> Optional[PlanStep]:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    # ----------------------------------------------------- updates
    def with_status(self, status: PlanStatus) -> "Plan":
        return replace(self, status=status)

    def with_steps(self, steps: Tuple[PlanStep, ...]) -> "Plan":
        return replace(self, steps=tuple(steps))

    def append_step(self, step: PlanStep) -> "Plan":
        return replace(self, steps=tuple([*self.steps, step]))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Plan",
            "plan_id": self.plan_id,
            "goal_id": self.goal_id,
            "status": self.status.value,
            "step_count": self.step_count,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "replan_count": self.replan_count,
            "parent_plan_id": self.parent_plan_id,
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# ExecutionContext
# ===========================================================================

@dataclass(frozen=True)
class ExecutionContext:
    """A read-only projection over the ContextService for one plan run.

    The :class:`PlanExecutor` receives an ``ExecutionContext`` and
    must NEVER mutate it.  If it needs to update world or task state,
    it goes through the ``ContextService`` (the only writer); the
    ``ExecutionContext`` is rebuilt for the next step.

    The ``context_service`` is the *interface seam*; the executor
    does not import :class:`ContextService` directly, it just calls
    the methods on whatever was injected.  In tests the injected
    service is a mock.
    """

    execution_id: str
    goal: Goal
    plan: Plan
    intent: Optional[Intent] = None
    current_step_id: Optional[str] = None
    completed_step_ids: Tuple[str, ...] = ()
    failed_step_ids: Tuple[str, ...] = ()
    context_service: Any = None   # the ContextService façade; Any to avoid a hard cycle
    capability_registry: Any = None  # the registry; same reason
    started_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Phase 4: optional cooperative cancellation token.  When
    # set and cancelled, the executor stops dispatching further
    # steps and returns a CANCELLED outcome.  The Agent passes
    # the same token the pipeline / engine created.
    cancellation_token: Any = None

    @property
    def progress(self) -> float:
        if self.plan.step_count == 0:
            return 0.0
        return min(1.0, len(self.completed_step_ids) / self.plan.step_count)

    def with_current_step(self, step_id: Optional[str]) -> "ExecutionContext":
        return replace(self, current_step_id=step_id)

    def with_completed(self, step_id: str) -> "ExecutionContext":
        if step_id in self.completed_step_ids:
            return self
        return replace(
            self,
            completed_step_ids=tuple([*self.completed_step_ids, step_id]),
        )

    def with_failed(self, step_id: str) -> "ExecutionContext":
        if step_id in self.failed_step_ids:
            return self
        return replace(
            self,
            failed_step_ids=tuple([*self.failed_step_ids, step_id]),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ExecutionContext",
            "execution_id": self.execution_id,
            "goal": self.goal.to_dict(),
            "plan": self.plan.to_dict(),
            "intent": self.intent.to_dict() if self.intent else None,
            "current_step_id": self.current_step_id,
            "completed_step_ids": list(self.completed_step_ids),
            "failed_step_ids": list(self.failed_step_ids),
            "progress": self.progress,
            "started_at": self.started_at,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Verifier (interface — Protocol)
# ===========================================================================

@runtime_checkable
class Verifier(Protocol):
    """The contract for any post-action / post-plan verifier.

    Two implementations will exist (and are exercised by the test
    suite, via mocks):

    1. **StepVerifier** — checks one step's :class:`ExpectedEffect`
       against an :class:`Observation` taken right after the step.
    2. **GoalVerifier** — checks the whole plan's outcome against
       the :class:`Goal`'s success criteria at the end.

    The verdict is a tri-state (R-8): passed, failed, or *uncertain*.
    Uncertainty is NOT a success; it routes to recovery.
    """

    name: str

    def verify(
        self,
        *,
        effect: ExpectedEffect,
        observation: Observation,
        context: ExecutionContext,
    ) -> "VerificationVerdict":
        """Compare ``observation`` against ``effect`` and return a verdict."""
        ...


@dataclass(frozen=True)
class VerificationVerdict:
    """A tri-state verdict from a :class:`Verifier` (R-8)."""

    passed: bool
    failed: bool
    uncertain: bool
    check_name: str
    expected: Any = None
    actual: Any = None
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Confidence in [0.0, 1.0] that the verdict is correct.
    # Defaults to 1.0 (high confidence) for explicit assertions.
    # Uncertain verdicts should carry a low confidence; failed
    # verdicts may carry 0.0 if the failure is unambiguous.
    confidence: float = 1.0

    def __post_init__(self) -> None:
        # Exactly one of the three flags must be true.
        flags = (self.passed, self.failed, self.uncertain)
        if sum(bool(f) for f in flags) != 1:
            raise ValueError(
                "VerificationVerdict must have exactly one of "
                f"passed/failed/uncertain set (got passed={self.passed}, "
                f"failed={self.failed}, uncertain={self.uncertain})"
            )
        # Clamp confidence to [0, 1] for sanity.  We don't reject
        # out-of-range values silently — clamp and log via the
        # caller.  R-10 immutability means we replace rather than
        # mutate, but __post_init__ runs before freezing.
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(
                f"VerificationVerdict.confidence must be in [0.0, 1.0], "
                f"got {self.confidence}"
            )

    def with_confidence(self, confidence: float) -> "VerificationVerdict":
        """Return a new verdict with ``confidence`` replaced.

        R-10 immutability: the original verdict is unchanged;
        mutation is expressed via ``with_*`` methods that return
        new instances.
        """
        return replace(self, confidence=float(confidence))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "VerificationVerdict",
            "passed": self.passed,
            "failed": self.failed,
            "uncertain": self.uncertain,
            "check_name": self.check_name,
            "confidence": self.confidence,
            "expected": self.expected,
            "actual": self.actual,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Failure
# ===========================================================================

class FailureKind(str, Enum):
    """The kind of failure a step / plan produced."""

    EXECUTION = "execution"           # the action itself failed
    VERIFICATION = "verification"     # the action ran but the world did not match
    TIMEOUT = "timeout"               # exceeded the deadline
    CANCELLED = "cancelled"           # cancelled before completion
    SAFETY = "safety"                 # refused by the safety policy
    UNKNOWN_CAPABILITY = "unknown_capability"  # router rejected the name
    INVALID_PARAMETERS = "invalid_parameters"
    PLAN_INFEASIBLE = "plan_infeasible"   # replan needed; current plan cannot finish
    INTERNAL = "internal"             # the orchestration layer itself crashed

    # Phase 1 / D23 + Phase 3: 6 UI failure kinds.  These are
    # the failure modes a desktop automation user actually
    # encounters.  The recovery engine maps each to a
    # deterministic action so the Agent is honest about what
    # went wrong and what it will do next.
    TARGET_NOT_FOUND = "target_not_found"   # vision / element not found
    FOCUS_FAILED = "focus_failed"           # window did not come to foreground
    WINDOW_NOT_READY = "window_not_ready"   # window still loading
    STALE_TARGET = "stale_target"           # target moved / reference invalid
    PROVIDER_FAILURE = "provider_failure"   # downstream service unavailable
    PERMISSION_FAILURE = "permission_failure"  # OS / UAC refused


@dataclass(frozen=True)
class Failure:
    """A structured description of a step or plan failure.

    The recovery engine consumes a :class:`Failure` and produces a
    :class:`RecoveryDecision`.  A ``Failure`` is *not* a Python
    exception; it is data.  The original exception, if any, is
    captured by string in ``cause`` for the audit log.
    """

    failure_id: str
    kind: FailureKind
    step_id: Optional[str] = None
    plan_id: Optional[str] = None
    message: str = ""
    cause: Optional[str] = None
    observation: Optional[Observation] = None
    attempt: int = 0
    is_retryable: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Failure",
            "failure_id": self.failure_id,
            "kind": self.kind.value,
            "step_id": self.step_id,
            "plan_id": self.plan_id,
            "message": self.message,
            "cause": self.cause,
            "observation": self.observation.to_dict() if self.observation else None,
            "attempt": self.attempt,
            "is_retryable": self.is_retryable,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# RecoveryDecision
# ===========================================================================

class RecoveryAction(str, Enum):
    """What the recovery engine decided to do about a failure."""

    RETRY = "retry"               # try the same step again
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    SKIP = "skip"                 # skip the failing step; continue
    REPLAN = "replan"             # ask the planner to make a new plan
    ABORT = "abort"               # stop the whole plan
    ASK_USER = "ask_user"         # surface the failure to the user
    GIVE_UP = "give_up"           # exhausted retries; mark plan failed


@dataclass(frozen=True)
class RecoveryDecision:
    """The output of the recovery engine for one :class:`Failure`."""

    decision_id: str
    action: RecoveryAction
    failure_id: str
    new_step: Optional[PlanStep] = None   # populated when action=REPLAN with a one-step fix
    backoff_s: float = 0.0                # for RETRY_WITH_BACKOFF
    ask_user_message: str = ""            # for ASK_USER
    rationale: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "RecoveryDecision",
            "decision_id": self.decision_id,
            "action": self.action.value,
            "failure_id": self.failure_id,
            "new_step": self.new_step.to_dict() if self.new_step else None,
            "backoff_s": self.backoff_s,
            "ask_user_message": self.ask_user_message,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
        }


# ===========================================================================
# Tiny utility: count_decorator
# ===========================================================================

def count_decorator(counter: Dict[str, int], key: str) -> Callable:
    """Return a decorator that increments ``counter[key]`` on each call.

    The orchestration interfaces are stateless; this helper lets
    implementations stamp lightweight metrics without pulling in
    Prometheus.  Tests use it to assert that a hook was visited.
    """
    def deco(fn: Callable) -> Callable:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            counter[key] = counter.get(key, 0) + 1
            return fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco
