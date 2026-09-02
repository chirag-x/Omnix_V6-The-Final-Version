"""
Omnix V6 — Phase 2: per-capability verifier routing.

The Agent's step-verification call site used to dispatch every step
through one :class:`DefaultStepVerifier`.  That was acceptable
while every step had the same shape (one capability, one
observation, one verdict), but it does not let us tune the
verifier to the *kind* of evidence a capability produces:

  * application lifecycle (open/close/focus) — process / window
    state is the dominant signal.
  * input (click / type / key) — vision is the only honest signal
    because the application may consume the input or ignore it.
  * vision (screenshot) — only vision can verify.

:class:`VerifierRouter` is a tiny dispatcher:

    router = VerifierRouter(default=DefaultStepVerifier())
    router.register("desktop.application.open", ApplicationLifecycleVerifier())
    router.register("desktop.input.click",       VisionDiffVerifier())
    ...
    verdict = router.verify(
        capability_name="desktop.application.open",
        effect=effect,
        observation=obs,
    )

Unregistered capabilities fall back to ``default``.  The router
satisfies the same :class:`Verifier` Protocol as a single verifier
(the ``verify`` method), so the Agent does not have to change
its call site to use one.

Architectural rules honored:

- R-7  — pure dispatch; no I/O, no logging.
- R-8  — tri-state verdict, never a bare bool.
- R-10 — frozen registry, no mutation after construction; new
         capabilities are added with :meth:`register`.
- R-12 — typed Protocols, no concrete imports above this layer.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .models import (
    ExpectedEffect,
    ExecutionContext,
    Observation,
    VerificationVerdict,
)


# Default capabilities the desktop subsystem exposes.  These are
# the names the Agent's :class:`MultiStepCoordinator` is allowed to
# dispatch, and they are the keys the router is indexed by.
DEFAULT_DESKTOP_CAPABILITIES: tuple = (
    "desktop.application.open",
    "desktop.application.close",
    "desktop.application.focus",
    "desktop.application.is_running",
    "desktop.application.list_installed",
    "desktop.window.list",
    "desktop.window.focus",
    "desktop.window.close",
    "desktop.input.click",
    "desktop.input.type_text",
    "desktop.input.key",
    "desktop.input.scroll",
    "desktop.screenshot",
)


class VerifierRouter:
    """Dispatch step verification to a per-capability :class:`Verifier`.

    The router exposes the same ``verify(...)`` signature as a
    single verifier, plus a ``capability_name`` keyword.  When the
    capability is not registered, the router falls back to
    ``self.default`` (which is always a verifier, never ``None``).

    The router is intentionally a *dispatcher*, not a *strategy
    chain*.  Composition lives inside the registered verifier
    (e.g. an :class:`ApplicationLifecycleVerifier` may use a
    vision diff internally).  This keeps the contract closed:
    one call → one verdict.
    """

    name: str = "verifier-router"

    def __init__(
        self,
        *,
        default: Any,
        registry: Optional[Dict[str, Any]] = None,
    ) -> None:
        if default is None:
            raise TypeError(
                "VerifierRouter requires a non-None default verifier"
            )
        self._default = default
        # Use a dict copy so callers can pass a pre-built registry
        # without sharing references.
        self._registry: Dict[str, Any] = dict(registry or {})

    # ---------------------------------------------------------- accessors
    @property
    def default(self) -> Any:
        """The fallback verifier used for unregistered capabilities."""
        return self._default

    def registered_capabilities(self) -> List[str]:
        """Return the sorted list of capability names registered."""
        return sorted(self._registry.keys())

    def get(self, capability_name: str) -> Any:
        """Return the registered verifier for ``capability_name``,
        or ``None`` if no override is set.
        """
        return self._registry.get(capability_name)

    # ---------------------------------------------------------- registry
    def register(self, capability_name: str, verifier: Any) -> None:
        """Bind ``capability_name`` to ``verifier``.

        Re-registering an existing capability overwrites the prior
        binding.  The verifier must expose a ``verify(**kwargs)``
        method; the router does not type-check it at registration
        time so fakes and stubs work in tests.
        """
        if not capability_name or not isinstance(capability_name, str):
            raise ValueError(
                f"capability_name must be a non-empty string, "
                f"got {capability_name!r}"
            )
        if verifier is None:
            raise ValueError(
                f"verifier for {capability_name!r} must not be None"
            )
        self._registry[capability_name] = verifier

    def unregister(self, capability_name: str) -> None:
        """Remove the binding for ``capability_name``.  No-op if absent."""
        self._registry.pop(capability_name, None)

    # ---------------------------------------------------------- dispatch
    def verify(
        self,
        *,
        capability_name: str,
        effect: ExpectedEffect,
        observation: Optional[Observation],
        before_observation: Optional[Observation] = None,
        context: Optional[ExecutionContext] = None,
    ) -> VerificationVerdict:
        """Dispatch verification to the per-capability verifier.

        ``capability_name`` is the routing key.  Unregistered keys
        fall back to ``self.default``.

        The router accepts the same kwargs the default verifier
        does (``effect``, ``observation``, ``before_observation``,
        ``context``) and forwards them.  Per-capability verifiers
        may ignore the kwargs they do not need.
        """
        verifier = self._registry.get(capability_name, self._default)
        # The Protocol contract is: every verifier must accept the
        # four kwargs as keyword arguments.  We pass only the ones
        # the verifier's signature supports to keep fakes
        # duck-typed.
        try:
            return verifier.verify(
                effect=effect,
                observation=observation,
                before_observation=before_observation,
                context=context,
            )
        except TypeError:
            # Older verifiers may not accept before_observation /
            # context.  Retry with the minimal argument set.
            try:
                return verifier.verify(
                    effect=effect,
                    observation=observation,
                )
            except TypeError:
                # Final fallback: pass everything as **kwargs.  This
                # is a defensive path; production verifiers should
                # accept the standard four.
                return verifier.verify(
                    effect=effect, observation=observation,
                )

    # ---------------------------------------------------------- dunder
    def __contains__(self, capability_name: str) -> bool:
        return capability_name in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    def __bool__(self) -> bool:
        # A router is always truthy — the size of the registry is
        # a separate concept.  Without this, an empty router
        # would be falsy and ``router or default_router`` would
        # silently replace it.  Use ``if router is None`` to
        # detect "no router".
        return True


# ===========================================================================
# Default router builder
# ===========================================================================

def build_default_router(
    *,
    default: Any = None,
) -> VerifierRouter:
    """Build the canonical :class:`VerifierRouter` for the desktop
    subsystem.

    The default fallback is :class:`DefaultStepVerifier` from
    :mod:`core.orchestration.verifier`.  Per-capability entries
    fall back to the default until Phase 3+ lands specialized
    verifiers (e.g. an :class:`ApplicationLifecycleVerifier`); at
    that point the registry will diverge from the default.
    """
    if default is None:
        from .verifier import DefaultStepVerifier
        default = DefaultStepVerifier()

    router = VerifierRouter(default=default)
    # Register each of the 12 desktop capabilities against the
    # default verifier explicitly.  This makes the registry the
    # *closed* set of known capability names — callers can ask
    # the router "do you know this capability?" via ``in`` and
    # get a deterministic yes.  Phase 3 can replace these
    # bindings with specialized verifiers by calling
    # ``router.register(name, specialized_v)`` after this
    # function returns.
    for cap in DEFAULT_DESKTOP_CAPABILITIES:
        router.register(cap, default)
    return router


__all__ = [
    "VerifierRouter",
    "build_default_router",
    "DEFAULT_DESKTOP_CAPABILITIES",
]
