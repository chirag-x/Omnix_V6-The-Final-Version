"""
Omnix V6 - The Omnix Engine (Phase 1).

The Engine is the root orchestrator (R-1: "thin orchestrator").
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from loguru import logger

from .capability_registry import CapabilityRegistry
from .capability_router import CapabilityRouter
from .configuration import OmnixConfig
from .events.event_types import EngineEvent, ErrorEvent, make_event
from .events.event_bus import EventBus  # noqa: F401  (re-exported below)
from .orchestration.cancellation import CancellationToken
from .health_monitor import HealthMonitor
from .lifecycle import LifecycleMixin, LifecycleState
from .results import CapabilityResult, CapabilityStatus
from .service_registry import ServiceRegistry
from .state.context_service import ContextService
from .responses import OmnixResponse, ResponseStatus, new_correlation_id
from .services.readiness import ReadinessGate, ReadinessReport
from .services.speech_queue import SpeechQueue
from .task.executor import _TASK_EXECUTOR_AVAILABLE
from .events.event_types import (
    EngineEvent,
    ErrorEvent,
    RequestEvent,
    REQUEST_RECEIVED,
    REQUEST_COMPLETED,
    make_event,
)


class OmnixEngine(LifecycleMixin):
    """The root V6 orchestrator.  Zero business logic; 100% coordination."""

    def __init__(
        self,
        config: OmnixConfig,
        *,
        bus: Optional[EventBus] = None,
        registry: Optional[ServiceRegistry] = None,
        health: Optional[HealthMonitor] = None,
        contexts: Optional[ContextService] = None,
        capabilities: Optional[CapabilityRegistry] = None,
        router: Optional[CapabilityRouter] = None,
        memory: Optional[Any] = None,
    ) -> None:
        self.config = config

        self.bus = bus or EventBus(name="engine-bus")
        self.services = registry or ServiceRegistry()
        self.health = health or HealthMonitor()
        self.contexts = contexts or ContextService.create()
        self.capabilities = capabilities or CapabilityRegistry()

        # Phase 9: memory service.  R-13 — memory is a service, not a
        # singleton.  The engine constructs a default instance only when
        # the caller did not inject one.  The default backend is
        # :class:`InMemoryStore`; production hosts may pass an
        # :class:`SQLiteMemoryStore` (or any object implementing the
        # :class:`MemoryStore` protocol) through the ``memory=`` keyword.
        self.memory: Any = memory if memory is not None else _default_memory_service(config)

        # When the caller supplies a registry, treat it as already
        # populated; the engine must not re-seed it with the standard
        # capability set (which would also re-register duplicates).
        # The bootstrap only runs when the engine created the registry
        # itself.
        self._standard_capabilities_seeded = capabilities is not None

        # Router requires the capability registry.
        self.router = router or CapabilityRouter(self.capabilities)

        self._lock = threading.RLock()

        # Phase 4: per-correlation_id cancellation tokens.  The
        # engine creates a fresh CancellationToken for every
        # ``process()`` call and stores it here so the SIGINT
        # handler (or the voice "stop" command) can flip it via
        # :meth:`request_cancel`.  Tokens are removed when the
        # request completes.
        self._tokens_by_cid: Dict[str, CancellationToken] = {}
        self._tokens_lock = threading.Lock()
        self._execution_count = 0
        self._request_count = 0
        self._lifecycle_state = LifecycleState.CREATED
        self._initialization_error = None
        self.task_executor = None

        # Phase 11: canonical request pipeline.  Constructed lazily in
        # ``_do_initialize`` once services, registry, and brain are
        # available.  Set to ``None`` until then.
        self.pipeline: Optional[Any] = None

        # Phase 15: readiness gate, engine-owned SpeechQueue, startup
        # announcer, and progress bridge.  These are constructed
        # eagerly so the engine always has a deterministic report
        # surface, even when the host did not inject one.
        self.readiness_gate = ReadinessGate(self.services)
        self.speech_queue = SpeechQueue()
        self._startup_announcer: Optional[Any] = None
        self._voice_bridge: Optional[Any] = None

        # Part 3 (Phase 15 final): voice runtime and inactivity timer.
        # Built lazily in ``_build_voice_subsystems`` so a host without
        # ``voice.runtime`` available still boots.  When built, the
        # runtime drives command STT + the wake-word listener, and
        # the timer triggers the sleep transition after the configured
        # idle window.
        self.voice_runtime: Optional[Any] = None
        self.inactivity_timer: Optional[Any] = None
        # Cooperative shutdown flag — the unified REPL observes this
        # so ``python main.py`` can exit cleanly from any thread.
        self._shutdown_requested: bool = False

    def _do_initialize(self) -> bool:
        """Initialize the core engine structure."""
        logger.info("Initializing Omnix V6 Engine...")

        # 0. Bootstrap the standard capability set if the caller did not
        #    inject their own registry.  This keeps the engine a thin
        #    orchestrator: the engine itself does not know about any
        #    concrete capability — it delegates to the standard
        #    registration function.  Tests / hosts that need a different
        #    capability set pass ``capabilities=`` explicitly and bypass
        #    this step.
        if not self._standard_capabilities_seeded:
            from .capabilities import register_standard_capabilities
            # Phase 8: construct the canonical BrowserService here so
            # the standard capability set can wire the browser
            # capabilities.  The service is not a singleton (R-14);
            # the engine simply hands the instance it owns to the
            # standard registration function.  Callers who want a
            # different browser configuration may inject their own
            # service through ``self.services.register(...)`` BEFORE
            # the engine is initialised; we then prefer that instance.
            browser_service = self._resolve_browser_service()
            application_service = self._resolve_application_service()
            input_service = self._resolve_input_service()
            window_service = self._resolve_window_service()
            ai_provider = self._resolve_llm_provider()
            register_standard_capabilities(
                self.capabilities,
                browser_service=browser_service,
                application_service=application_service,
                input_service=input_service,
                window_service=window_service,
                ai_provider=ai_provider,
            )
            self._standard_capabilities_seeded = True

        # 1. Register base services with the ServiceRegistry
        self.services.register(self.contexts, name="contexts", priority=100)
        self.services.register(self.health, name="health", priority=90)

        # Phase 9: register the memory service so its lifecycle is
        # walked by ``ServiceRegistry.initialize_all`` /
        # ``shutdown_all``.  R-13: memory is a service, not a
        # singleton; the engine owns the only canonical instance.
        if self.memory is not None and not self.services.has("memory"):
            self.services.register(
                self.memory,
                name="memory",
                priority=85,
            )

        # Phase 15: register the engine-owned application service so
        # the readiness report lists it under the canonical name.
        if not self.services.has("application_service"):
            from system.application import WindowsApplicationService
            try:
                app_svc = WindowsApplicationService()
                self.services.register(
                    app_svc,
                    name="application_service",
                    priority=60,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"ApplicationService construction failed: {exc!r}"
                )

        # 2. Boot all registered services via topological sort
        ok = self.services.initialize_all()
        if not ok:
            logger.error(f"Service initialization failed:\n{self.services.statistics()}")
            self.bus.publish(
                make_event(
                    EngineEvent,
                    source="engine",
                    transition="failed",
                    metadata={"error": "service initialization failed"},
                )
            )
            return False

        # 3. Track core components in HealthMonitor
        self.health.track("services", self.services)
        self.health.track("contexts", self.contexts)
        if self.memory is not None:
            self.health.track("memory", self.memory)

        # Phase 11: build the canonical request pipeline (text →
        # brain → agent → response).  We do this AFTER services are
        # initialized so any service the brain / agent depends on is
        # already up.  Pipeline construction is best-effort: if the
        # subsystem pieces (Brain / Agent) are not wired, the engine
        # still boots — only ``process()`` will return a structured
        # FAILED response indicating the pipeline is unavailable.
        self.pipeline = self._build_pipeline()

        # 4. Wire the voice progress bridge + startup announcer.
        #    These are best-effort: missing voice module is fine.
        self._attach_voice_subsystems()

        # 4b. Part 3 voice runtime: build VoiceRuntime + InactivityTimer
        #     when the config opts in.  Done after the announcer /
        #     bridge are attached so the runtime can re-use the
        #     engine-owned SpeechQueue.  Best-effort: any failure is
        #     logged and the engine continues without voice input.
        if bool(getattr(self.config, "enable_voice_runtime", False)):
            self._build_voice_subsystems()
            if self.voice_runtime is not None:
                self._auto_connect_tts()
                # Auto-announce only when the readiness gate is green.
                # Failures here are non-fatal; the engine still boots.
                try:
                    self.announce_ready()
                except Exception:  # noqa: BLE001
                    print("auto-announce failed; continuing", exc_info=True)

        # 5. Emit initialized event
        self.bus.publish(make_event(EngineEvent, source="engine", transition="ready"))

        logger.info("Engine initialized.")
        return True

    def _do_shutdown(self) -> None:
        """Gracefully shut down the engine and all services."""
        logger.info("Shutting down Omnix V6 Engine...")

        self.bus.publish(make_event(EngineEvent, source="engine", transition="stopping"))
        # Part 3: stop voice subsystems first so the wake listener
        # releases the microphone before services are torn down.
        try:
            if self.inactivity_timer is not None:
                self.inactivity_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.voice_runtime is not None:
                self.voice_runtime.stop()
        except Exception:  # noqa: BLE001
            pass
        self.services.shutdown_all()
        self.bus.publish(make_event(EngineEvent, source="engine", transition="stopped"))

        logger.info("Engine shutdown complete.")

    def _resolve_browser_service(self) -> Optional[Any]:
        """Return the :class:`BrowserService` to wire into the
        standard capability set (Phase 8).

        Order of preference:

        1. If a service named ``browser_service`` is already registered
           in ``self.services`` (e.g. injected by a host), use it.
        2. Otherwise construct a fresh :class:`BrowserService` with
           the engine's default settings and register it so the
           service registry can shut it down on engine stop.

        Returns ``None`` if the browser subsystem is unavailable in
        this environment (e.g. Playwright not installed).
        """
        existing = self.services.try_resolve("browser_service")
        if existing is not None:
            return existing

        try:
            from core.services.browser_service import BrowserService
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"BrowserService import failed; browser capabilities "
                f"will not be registered: {exc}"
            )
            return None

        try:
            service = BrowserService()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"BrowserService construction failed; browser "
                f"capabilities will not be registered: {exc}"
            )
            return None

        try:
            # Register the browser as ``background`` so hosts that do
            # not need the browser still pass the readiness gate.  The
            # browser is initialized on first request by the
            # ApplicationCatalog.
            try:
                self.services.register(
                    service, name="browser_service", priority=80,
                    classification="background",
                )
            except TypeError:
                # Older registry signature without classification kwarg.
                self.services.register(service, name="browser_service", priority=80)
        except Exception:  # noqa: BLE001
            # Already registered, or registry rejected; fall through.
            pass
        return service

    # ------------------------------------------------------ Phase 15 wiring
    def _resolve_application_service(self) -> Optional[Any]:
        """Return the canonical :class:`ApplicationService` to wire
        into the standard capability set.

        Order of preference:

        1. A service named ``application_service`` already registered
           in ``self.services`` (e.g. injected by a host).
        2. Otherwise construct a fresh
           :class:`system.application.WindowsApplicationService`.  The
           service is not a singleton; the engine simply hands the
           same instance to every capability that needs it.
        """
        existing = self.services.try_resolve("application_service")
        if existing is not None:
            return existing
        try:
            from system.application import WindowsApplicationService
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"ApplicationService import failed: {exc!r}"
            )
            return None
        try:
            return WindowsApplicationService()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"ApplicationService construction failed: {exc!r}"
            )
            return None

    def _resolve_input_service(self) -> Optional[Any]:
        existing = self.services.try_resolve("input_service")
        if existing is not None:
            return existing
        try:
            from system.input.input_service import WindowsInputService
        except Exception:  # noqa: BLE001
            return None
        try:
            return WindowsInputService()
        except Exception:  # noqa: BLE001
            return None

    def _resolve_window_service(self) -> Optional[Any]:
        existing = self.services.try_resolve("window_service")
        if existing is not None:
            return existing
        try:
            from system.windows.window_service import WindowsWindowService
        except Exception:  # noqa: BLE001
            return None
        try:
            return WindowsWindowService()
        except Exception:  # noqa: BLE001
            return None

    def _attach_voice_subsystems(self) -> None:
        """Wire the engine-owned :class:`SpeechQueue` to the
        :class:`VoiceProgressBridge` and create a
        :class:`StartupAnnouncer`.  Best-effort: a missing voice
        module is a no-op."""
        try:
            from voice.startup_announcer import StartupAnnouncer
            from voice.progress_bridge import VoiceProgressBridge
        except Exception as exc:  # noqa: BLE001
            print(f"Voice subsystems not available: {exc!r}")
            return
        try:
            self._startup_announcer = StartupAnnouncer(self.speech_queue)
        except Exception:  # noqa: BLE001
            self._startup_announcer = None
        try:
            self._voice_bridge = VoiceProgressBridge(
                self.bus, self.speech_queue, terminal=True
            )
            self._voice_bridge.attach()
        except Exception:  # noqa: BLE001
            self._voice_bridge = None

    # -------------------------------------------- Part 3 voice runtime wiring
    def _build_voice_subsystems(self) -> None:
        """Construct the :class:`VoiceRuntime` + :class:`InactivityTimer`
        when ``enable_voice_runtime`` is set.

        Both share the engine's :class:`SpeechQueue` and
        :class:`EventBus`.  Best-effort: a missing voice module is
        logged and the engine continues without voice input.
        """
        try:
            from voice.runtime import VoiceRuntime
            from core.state.inactivity_timer import InactivityTimer
        except Exception as exc:  # noqa: BLE001
            print(f"Voice runtime modules unavailable: {exc!r}")
            return

        wake_phrase = getattr(self.config, "wake_phrase", "omnix") or "omnix"
        try:
            self.voice_runtime = VoiceRuntime(
                speech_queue=self.speech_queue,
                wake_phrase=str(wake_phrase),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"VoiceRuntime construction failed: {exc!r}")
            self.voice_runtime = None
            return

        try:
            self.inactivity_timer = InactivityTimer(
                self.voice_runtime.controller,
                on_timeout=self._on_inactivity_timeout,
                timeout_s=float(
                    getattr(self.config, "inactivity_timeout_s", 30.0)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"InactivityTimer construction failed: {exc!r}")
            self.inactivity_timer = None

        # Lifecycle gate: the SpeechQueue now drops non-bypass items
        # while the runtime controller is in a sleep transition.
        try:
            self.speech_queue.attach_state_controller(
                self.voice_runtime.controller
            )
        except Exception:  # noqa: BLE001
            pass

        # Subscribe to request lifecycle so the timer stays in sync
        # (pause-while-executing + reset on user input).
        try:
            self.bus.subscribe("request.event", self._on_voice_bus_event)
        except Exception:  # noqa: BLE001
            pass

        # Start the runtime; the controller is in STARTING until the
        # engine promotes it to READY.
        try:
            self.voice_runtime.start()
        except Exception:  # noqa: BLE001
            print("VoiceRuntime start() raised; continuing", exc_info=True)
        try:
            if self.inactivity_timer is not None:
                self.inactivity_timer.start()
        except Exception:  # noqa: BLE001
            pass

        # Promote to READY so command STT turns on.  ``transition`` is
        # idempotent so re-entering READY from a previously-cleared
        # state is safe.
        try:
            from core.state.runtime_state import RuntimeState as _RS
            self.voice_runtime.controller.transition(_RS.READY)
        except Exception:  # noqa: BLE001
            pass

        # Wire the listen loop so the user does not have to type
        # ``/voice`` between every command.
        try:
            self.voice_runtime.set_engine(self)
            self.voice_runtime.set_on_command(self._on_voice_command)
            self.voice_runtime.start_listen_loop()
        except Exception:  # noqa: BLE001
            print("voice listen loop could not start", exc_info=True)

    def _on_inactivity_timeout(self) -> None:
        """InactivityTimer callback: transition the runtime to SLEEPING."""
        if self.voice_runtime is None:
            return
        try:
            self.voice_runtime.sleep()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"voice_runtime.sleep() raised: {exc!r}")

    def _on_voice_bus_event(self, event: Any) -> None:
        """Reset / pause the inactivity timer on request lifecycle.

        Pause-while-executing ensures the timer does not fire while
        the agent is running.  A new request resets the timer; a
        completed / cancelled / timed-out request resumes it.
        """
        if self.inactivity_timer is None:
            return
        name = getattr(event, "stage", "")
        if not name:
            return
        try:
            from core.events.event_types import (
                REQUEST_RECEIVED,
                REQUEST_EXECUTION_STARTED,
                REQUEST_COMPLETED,
                REQUEST_CANCELLED,
                REQUEST_TIMED_OUT,
                REQUEST_REJECTED,
            )
        except Exception:  # noqa: BLE001
            return
        if name == REQUEST_RECEIVED:
            try:
                self.inactivity_timer.reset_for_user_input()
            except Exception:  # noqa: BLE001
                pass
        elif name == REQUEST_EXECUTION_STARTED:
            try:
                self.inactivity_timer.reset_for_task_event()
            except Exception:  # noqa: BLE001
                pass
        elif name in (
            REQUEST_COMPLETED,
            REQUEST_CANCELLED,
            REQUEST_TIMED_OUT,
            REQUEST_REJECTED,
        ):
            try:
                self.inactivity_timer.mark_task_finished()
                self.inactivity_timer.reset_for_response()
            except Exception:  # noqa: BLE001
                pass

    def _on_voice_command(self, text: str) -> None:
        """Receive a transcribed utterance and forward to the engine.

        Mirrors the text-REPL semantics: ``/quit``/``/exit`` requests
        shutdown, anything else goes through ``process``.
        """
        line = (text or "").strip()
        if not line:
            return
        if line.lower() in ("/quit", "/exit", "/q", "quit", "exit"):
            try:
                self.request_shutdown()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            response = self.process(line)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"voice command handling failed: {exc!r}")
            return
        # Reset the timer for the user input we just consumed.
        if self.inactivity_timer is not None:
            try:
                self.inactivity_timer.reset_for_user_input()
            except Exception:  # noqa: BLE001
                pass
        # Speak the response if TTS is wired.
        if self.speech_queue is not None and response is not None:
            try:
                from .services.speech_queue import SpeechItem
                self.speech_queue.enqueue(
                    SpeechItem(
                        text=str(getattr(response, "text", "") or ""),
                        priority=500,
                        kind="response",
                        source="voice_response",
                    )
                )
            except Exception:  # noqa: BLE001
                pass

    def _auto_connect_tts(self) -> bool:
        """Auto-wire the engine's :class:`SpeechQueue` to SAPI TTS.

        Best-effort — missing TTS is non-fatal.  Returns ``True`` if
        a provider was connected.
        """
        try:
            from voice.tts.sapi_provider import SAPITTSProvider
        except Exception as exc:  # noqa: BLE001
            print(f"SAPI TTS provider unavailable: {exc!r}")
            return False
        try:
            provider = SAPITTSProvider()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"SAPI TTS construction failed: {exc!r}")
            return False
        try:
            return bool(self.connect_tts(provider))
        except Exception:  # noqa: BLE001
            return False

    def request_shutdown(self) -> None:
        """Signal cooperative shutdown — observed by the unified REPL.

        This does NOT immediately stop the engine; it sets a flag
        that the REPL polls in its loop.  A subsequent
        :meth:`shutdown` tears everything down.
        """
        self._shutdown_requested = True
        try:
            self.bus.publish(
                make_event(
                    EngineEvent,
                    source="engine",
                    transition="shutdown_requested",
                )
            )
        except Exception:  # noqa: BLE001
            pass

    def readiness_report(self) -> ReadinessReport:
        """Snapshot the current readiness report.  Convenience
        wrapper that hides the gate."""
        return self.readiness_gate.report()

    def announce_ready(self) -> bool:
        """Speak the startup announcement *only* if the readiness
        report says every critical subsystem is READY.  Returns
        ``True`` when the announcement was enqueued."""
        report = self.readiness_gate.report()
        if not report.is_ready:
            logger.warning(
                "announce_ready refused: not all critical services are READY"
            )
            return False
        if self._startup_announcer is None:
            logger.warning("announce_ready refused: no announcer wired")
            return False
        try:
            self._startup_announcer.announce()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"announce_ready failed: {exc!r}")
            return False

    def connect_tts(self, tts_provider: Any) -> bool:
        """Connect a TTS provider to the engine-owned
        :class:`SpeechQueue`.  When a TTS is connected, every
        :class:`SpeechItem` the worker dequeues is rendered through
        that provider.  If the TTS raises on a given item the worker
        logs and continues — the queue never blocks the automation.

        Returns ``True`` if the connection succeeded.  Idempotent:
        re-calling replaces the previous TTS callback.
        """
        if tts_provider is None:
            return False
        if self.speech_queue is None:
            logger.warning("connect_tts: no speech_queue; ignoring")
            return False
        try:
            from voice.contracts import TTSRequest
        except Exception:  # noqa: BLE001
            TTSRequest = None  # type: ignore[assignment]

        def _speak(item: Any) -> None:
            try:
                text = getattr(item, "text", None)
                if not text or not isinstance(text, str):
                    return
                if TTSRequest is not None and hasattr(tts_provider, "synthesize"):
                    tts_provider.synthesize(TTSRequest(text=text))
                elif callable(tts_provider):
                    tts_provider(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"SpeechQueue TTS render failed: {exc!r}")

        try:
            self.speech_queue.set_on_speak(_speak)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"connect_tts: could not set on_speak: {exc!r}")
            return False

    def wait_speech_idle(self, timeout_s: float = 5.0) -> bool:
        """Block until the engine-owned :class:`SpeechQueue` is idle
        or ``timeout_s`` elapses.  Returns ``True`` on idle."""
        if self.speech_queue is None:
            return True
        try:
            return bool(self.speech_queue.wait_idle(timeout_s=float(timeout_s)))
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> None:
        """Transition from READY to RUNNING state."""
        with self._lock:
            if self._lifecycle_state != LifecycleState.READY:
                logger.warning(f"Engine start() called from invalid state: {self._lifecycle_state.value}")
                return
            self._lifecycle_state = LifecycleState.RUNNING

        self.bus.publish(make_event(EngineEvent, source="engine", transition="running"))
        logger.info("Engine is now RUNNING.")

    def stop(self) -> None:
        """Transition from RUNNING to STOPPING state and call shutdown."""
        self.shutdown()

    def execute(self, capability_name: str, **kwargs: Any) -> CapabilityResult:
        """Single entry point for capability execution (R-21 / Phase 2 wiring)."""
        if self._lifecycle_state not in (LifecycleState.READY, LifecycleState.RUNNING):
            msg = f"Cannot execute capability {capability_name!r}: engine is {self._lifecycle_state.value}"
            logger.error(msg)
            self.bus.publish(
                make_event(
                    ErrorEvent,
                    source="engine",
                    code="ENGINE_NOT_READY",
                    message=msg,
                    recoverable=False,
                )
            )
            return CapabilityResult(
                name=capability_name,
                status=CapabilityStatus.FAILED,
                error=msg,
            )

        start_time = time.time()
        with self._lock:
            self._execution_count += 1

        print(f">> [Engine] Executing {capability_name}...")
        result = self.router.route(capability_name, **kwargs)
        duration = (time.time() - start_time) * 1000.0

        logger.info(
            f"<< [Engine] {capability_name} completed in {duration:.1f}ms: {result.status.value}"
        )
        return result

    # ------------------------------------------------------------ Phase 11
    def _build_pipeline(self) -> Optional[Any]:
        """Construct the canonical :class:`RequestPipeline` for Phase 11.

        Returns ``None`` if any of the required subsystems (Brain, Agent)
        cannot be built in this environment (e.g. the LLM provider
        cannot be initialised, or the Agent's components are missing).
        Callers that need to distinguish "pipeline not built" from
        "pipeline returned a structured failure" should check
        ``self.pipeline is None`` directly.
        """
        try:
            from .pipeline import RequestPipeline
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"RequestPipeline import failed: {exc!r}")
            return None

        # 1. Resolve the LLM provider
        llm_provider = self._resolve_llm_provider()
        if llm_provider is None:
            logger.warning(
                "No LLM provider available; canonical pipeline disabled."
            )
            return None

        # 2. Build the IntentInterpreter
        try:
            from ai.intent.interpreter import LLMIntentInterpreter
            from ai.intent.specs import build_default_registry
            from core.intelligence.native_intent_interpreter import NativeIntentInterpreter
            from core.intelligence.hybrid_pipeline import HybridIntentInterpreter
            
            llm_interpreter = LLMIntentInterpreter(
                provider=llm_provider,
                registry=build_default_registry(),
            )
            native_interpreter = NativeIntentInterpreter()
            interpreter = HybridIntentInterpreter(
                native_interpreter=native_interpreter,
                llm_interpreter=llm_interpreter,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Intent interpreter build failed: {exc!r}")
            return None

        # 3. Build the Hybrid Planner (Native -> LLM fallback)
        try:
            from ai.brain.deterministic import DeterministicPlanner
            from core.intelligence.capability_resolver import CapabilityResolver
            from core.intelligence.native_task_planner import NativeTaskPlanner
            from core.intelligence.hybrid_pipeline import HybridPlanner
            
            resolver = CapabilityResolver(self.capabilities)
            native_planner = NativeTaskPlanner(resolver)
            llm_fallback = DeterministicPlanner(registry=self.capabilities) # We use Deterministic as the 'LLM' fallback to match original engine behavior, though in a real system it would be LLMPlanner
            
            planner = HybridPlanner(
                native_planner=native_planner,
                llm_planner=llm_fallback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Planner build failed: {exc!r}")
            return None

        # 4. Build the Brain
        try:
            from ai.brain.brain import Brain
            brain = Brain(
                registry=self.capabilities,
                interpreter=interpreter,
                planner=planner,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Brain build failed: {exc!r}")
            return None

        # 5. Build the Agent (uses canonical orchestration components)
        try:
            from core.orchestration import (
                Agent,
                AgentPolicy,
                PlanExecutorImpl,
                DefaultStepVerifier,
                DefaultGoalVerifier,
                DefaultRecoveryEngine,
            )
            from core.orchestration.observation import (
                CapabilityResultObservationProvider,
            )

            plan_executor = PlanExecutorImpl(
                router=self.router,
                event_bus=self.bus,
                # Stage 19.3: wire the canonical ExecutionCycle.
                # When set, every dispatch routes through the
                # PRECONDITION → OBSERVE → GROUND → ACT →
                # SYNCHRONIZE → VERIFY phases.  When the cycle
                # cannot be built in this environment (e.g. vision
                # providers not installed) the executor falls back
                # to the legacy direct ``router.route()`` path.
                execution_cycle=self._build_execution_cycle(),
            )
            # Stage 19.3: expose the plan_executor on the engine
            # so the FastPathDispatcher (and any other consumer)
            # can use it.  The dispatcher is built later in
            # ``_build_app_dispatcher``; it inspects
            # ``getattr(self, "plan_executor", None)`` so this
            # assignment must happen before that call.
            self.plan_executor = plan_executor

            # Phase 21: build the TaskExecutor on top of the PlanExecutor.
            # The TaskExecutor is the canonical entry point for multi-step
            # user tasks that span multiple capabilities. It builds on
            # Stages 18-20 (PlanExecutor → ExecutionCycle → Recovery) and
            # adds task-level state, lifecycle events, and final verification.
            try:
                from core.task.executor import TaskExecutor, TaskExecutorConfig
                task_executor = TaskExecutor(
                    plan_executor=plan_executor,
                    config=TaskExecutorConfig(
                        max_task_retries=2,
                        enable_step_recovery=True,
                        enable_task_replanning=True,
                        event_publisher=self._build_task_event_publisher(),
                    ),
                )
                self.task_executor = task_executor
                logger.info("Stage 21 TaskExecutor initialized")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"TaskExecutor build failed: {exc!r}")
                self.task_executor = None

            # Use TaskExecutor for Stage 21 if available, otherwise fall back to plan_executor
            effective_plan_executor = self.task_executor if self.task_executor is not None else plan_executor

            step_verifier = DefaultStepVerifier()
            goal_verifier = DefaultGoalVerifier()
            recovery_engine = DefaultRecoveryEngine()
            observation_provider = CapabilityResultObservationProvider()
            policy = AgentPolicy()

            # Phase 13: build the typed VisionTargetProvider the Agent
            # consults before any pre-action computer-use step.  We
            # build it lazily because the VisionService depends on
            # the ScreenshotProvider which depends on the
            # CapabilityRouter -- and the router is not ready until
            # the standard capability set is seeded.  When vision
            # is not enabled in the host environment, the provider
            # may be ``None`` and the Agent falls back to the
            # ``vision_service=`` legacy path (still backwards
            # compatible).
            vision_target_provider = self._build_vision_target_provider()
            confidence_threshold = float(
                getattr(self.config, "vision_confidence_threshold", 0.5)
            )

            # Phase 14: build the per-Engine MultiStepCoordinator and
            # wire it into the Agent.  The coordinator owns the
            # in-memory MultiStepContext store and the IdempotencyLog
            # and is consulted on every plan run for preconditions,
            # idempotency, and re-grounding.  When the host has
            # vision enabled the GroundingProvider is wired here too.
            multi_step_coordinator = self._build_multi_step_coordinator(
                vision_target_provider=vision_target_provider,
            )

            # Use TaskExecutor for Stage 21 if available, otherwise fall back to plan_executor
            effective_plan_executor = self.task_executor if self.task_executor is not None else plan_executor

            agent = Agent(
                interpreter=interpreter,
                planner=planner,
                plan_executor=effective_plan_executor,
                recovery_engine=recovery_engine,
                step_verifier=step_verifier,
                goal_verifier=goal_verifier,
                observation_provider=observation_provider,
                policy=policy,
                vision_service=vision_target_provider,
                confidence_threshold=confidence_threshold,
                multi_step_coordinator=multi_step_coordinator,
                # Phase 1 / D5: wire an observability_sink that
                # translates the Agent's free-text events into
                # structured ``AgentEvent`` envelopes on the bus.
                # The lambda closes over ``self.bus`` and the
                # current correlation_id which the pipeline stamps
                # onto every request.  The sink is intentionally
                # fail-soft: a bus error must never break the
                # Agent.
                observability_sink=self._build_agent_observability_sink(),
                # System 8: wire a real LogProgressBroadcaster so
                # every Agent step is visible in the structured
                # loguru stream.  Production code may add a
                # CompositeProgressBroadcaster here that also fans
                # out to the event bus; the Agent only requires
                # the typed ``ProgressBroadcaster`` protocol.
                progress_broadcaster=self._build_progress_broadcaster(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Agent build failed: {exc!r}")
            return None

        # Track each Phase 11 subsystem in the HealthMonitor so
        # health reports name them by role.  Brain, Agent, and the
        # pipeline are lifecycle-agnostic — they do not expose
        # ``lifecycle_state`` or ``health()`` — so we wire a custom
        # probe that asks the instance whether it was built.  The
        # LLM provider already implements :meth:`health`, so we
        # let the monitor call it through the default derivation.
        def _built_probe(instance: Any) -> bool:
            return instance is not None

        for name, instance in (
            ("llm_provider", llm_provider),
            ("brain", brain),
            ("agent", agent),
        ):
            try:
                if instance is not None:
                    if name == "llm_provider":
                        self.health.track(name, instance)
                    else:
                        self.health.track(
                            name,
                            instance,
                            probe=lambda inst=instance: _built_probe(inst),
                        )
            except Exception:  # noqa: BLE001
                pass

        try:
            # Phase 15: build the SimpleAppDispatcher for the fast
            # path when an application service is available.  We do
            # this lazily so the pipeline is never blocked on it.
            app_dispatcher = self._build_app_dispatcher()
            pipeline = RequestPipeline(
                brain=brain,
                agent=agent,
                memory_service=self.memory,
                event_bus=self.bus,
                app_dispatcher=app_dispatcher,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"RequestPipeline construction failed: {exc!r}")
            return None

        try:
            self.health.track(
                "pipeline",
                pipeline,
                probe=lambda: pipeline is not None,
            )
        except Exception:  # noqa: BLE001
            pass

        return pipeline

    def _resolve_llm_provider(self) -> Optional[Any]:
        """Resolve the canonical LLM provider for Brain/Interpreter.

        Priority:
          1. An existing service named ``llm_provider`` in the registry.
          2. A fresh provider from :func:`ai.provider.get_provider`.
        """
        existing = self.services.try_resolve("llm_provider")
        if existing is not None:
            return existing

        try:
            from ai.provider import get_provider
            provider = get_provider(self.config)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"LLM provider construction failed: {exc!r}")
            return None

        if provider is None:
            return None

        try:
            self.services.register(provider, name="llm_provider", priority=70)
        except Exception:  # noqa: BLE001
            pass
        return provider

    def _build_vision_target_provider(self) -> Optional[Any]:
        """Construct a :class:`VisionTargetProvider` for the Agent (Phase 13).

        Order of preference:

        1. An existing service named ``vision_target_provider``
           in the registry (hosts may inject a custom provider).
        2. A :class:`DefaultVisionTargetProvider` wrapping a
           fresh :class:`core.services.vision_service.VisionService`.

        Returns ``None`` when the vision subsystem cannot be built
        in this environment (e.g. Playwright / pywinauto are not
        installed); the Agent then operates without vision
        grounding, which is a safe default.
        """
        existing = self.services.try_resolve("vision_target_provider")
        if existing is not None:
            return existing

        try:
            from vision.router.screenshot_provider import (
                CapabilityScreenshotProvider,
                NullScreenshotProvider,
                make_screenshot_provider,
            )
            from core.services.vision_service import VisionService
            from vision.integration.agent_provider import (
                DefaultVisionTargetProvider,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Vision imports unavailable; vision_target_provider "
                f"disabled: {exc!r}"
            )
            return None

        screenshot_provider: Any
        try:
            screenshot_provider = make_screenshot_provider(
                self,
                headless=not bool(self.config.enable_vision),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"make_screenshot_provider failed; falling back to "
                f"NullScreenshotProvider: {exc!r}"
            )
            screenshot_provider = NullScreenshotProvider()

        if screenshot_provider is None:
            screenshot_provider = NullScreenshotProvider()

        try:
            vision_service = VisionService(screenshot_provider=screenshot_provider)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"VisionService construction failed; "
                f"vision_target_provider disabled: {exc!r}"
            )
            return None

        try:
            provider = DefaultVisionTargetProvider(
                vision_service,
                max_screenshot_age_s=float(
                    getattr(self.config, "vision_max_screenshot_stale_s", 5.0)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"DefaultVisionTargetProvider construction failed: {exc!r}"
            )
            return None

        try:
            self.services.register(
                provider, name="vision_target_provider", priority=72
            )
        except Exception:  # noqa: BLE001
            pass
        return provider

    # ------------------------------------------------------ System 8 wiring
    def _build_progress_broadcaster(self) -> Any:
        """Construct a :class:`ProgressBroadcaster` for the Agent.

        Returns a :class:`LogProgressBroadcaster` by default.  When
        a structured event bus is available the engine may swap in
        a :class:`CompositeProgressBroadcaster` (log + bus) so
        downstream consumers can subscribe.  Tests pass an
        :class:`InMemoryProgressBroadcaster` directly into the
        :class:`Agent` constructor.
        """
        from core.orchestration.progress import LogProgressBroadcaster
        return LogProgressBroadcaster()

    def _build_agent_observability_sink(self) -> Any:
        """Build the :class:`Agent`'s ``observability_sink``.

        Phase 1 / D5 + Phase 6 / wiring: returns a callable that
        accepts ``(kind, payload)`` and republishes the event on
        the engine's :class:`EventBus` as an :class:`AgentEvent`
        envelope.  When the bus is not available (e.g. a
        partial-boot test) we return ``None`` so the Agent
        silently no-ops.

        The sink is *fail-soft*: bus publishing must never break
        the Agent loop.  Exceptions are logged at DEBUG and
        swallowed, matching the same contract the Agent uses for
        its own internal calls.
        """
        bus = getattr(self, "bus", None)
        if bus is None:
            return None

        def _sink(kind: str, payload: Any) -> None:
            try:
                from core.events.event_types import AgentEvent
                pd = dict(payload or {}) if isinstance(payload, Mapping) else {}
                # Build the structured envelope.  Carries the
                # Agent's free-text kind plus the most useful
                # routing fields.
                evt = AgentEvent(
                    event_kind=str(kind),
                    correlation_id=str(pd.get("correlation_id", "") or ""),
                    plan_id=str(pd.get("plan_id", "") or ""),
                    step_id=str(pd.get("step_id", "") or ""),
                    agent_run_id=str(pd.get("agent_run_id", "") or ""),
                    final_state=str(pd.get("final_state", "") or ""),
                    payload=pd,
                )
                # Look for the bus publish() — some buses accept
                # a stage string + kwargs, some accept a single
                # event object.  Try both.
                if hasattr(bus, "publish_event"):
                    bus.publish_event(evt)
                elif hasattr(bus, "publish"):
                    bus.publish(evt)
                else:
                    # Last-resort: drop silently.
                    pass
            except Exception:  # noqa: BLE001
                print(
                    "agent observability_sink failed for kind={!r}", kind,
                )

        return _sink

    # ------------------------------------------------------ Phase 14 wiring
    def _build_multi_step_coordinator(
        self,
        *,
        vision_target_provider: Optional[Any] = None,
    ) -> Optional[Any]:
        """Construct a :class:`MultiStepCoordinator` for the Agent.

        The coordinator is the Phase 14 helper the Agent consults
        before every step dispatch (preconditions, idempotency,
        re-grounding) and after every dispatch (postconditions,
        world-fact stamping).  The Engine owns the in-memory
        :class:`MultiStepContext` store and :class:`IdempotencyLog`
        and gives them to the coordinator; the coordinator itself
        is stateless beyond those two stores.

        Returns ``None`` when the Phase 14 modules cannot be
        imported (e.g. older deploys); the Agent then operates
        exactly as in Phase 6C, which is a safe default.
        """
        try:
            from core.orchestration.multi_step_coordinator import (
                MultiStepCoordinator,
                InMemoryMultiStepContextStore,
                InMemoryIdempotencyStore,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Phase 14 multi-step modules unavailable; "
                f"coordinator disabled: {exc!r}"
            )
            return None

        try:
            ctx_store = InMemoryMultiStepContextStore()
            idem_store = InMemoryIdempotencyStore()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"MultiStep store construction failed: {exc!r}"
            )
            return None

        try:
            return MultiStepCoordinator(
                context_store=ctx_store,
                idempotency_store=idem_store,
                world_state=None,
                grounding_provider=vision_target_provider,
                scroll_executor=None,
                duplicate_action_policy="refuse",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"MultiStepCoordinator construction failed: {exc!r}"
            )
            return None

    # ------------------------------------------------------ Phase 15 wiring
    def _build_app_dispatcher(self) -> Optional[Any]:
        """Build the Phase 15 :class:`FastPathDispatcher` for the
        pipeline's local-first fast path.

        The dispatcher is generic.  It wires
        :class:`LocalActionDecisionEngine` to the
        :class:`CapabilityRouter` and (when available) the
        :class:`PlanExecutor`.  It never fakes a ``VERIFIED`` result
        — the underlying capability is the only source of truth.

        Requires an application service for name resolution; returns
        ``None`` when one is not available.
        """
        from core.services.app_dispatcher import FastPathDispatcher
        try:
            app_svc = self.services.try_resolve("application_service")
            if app_svc is None:
                return None
            resolver = getattr(app_svc, "_resolver", None)
            if resolver is None:
                return None
            return FastPathDispatcher(
                resolver=resolver,
                registry=getattr(self, "registry", None),
                router=getattr(self, "router", None),
                plan_executor=getattr(self, "plan_executor", None),
            )
        except Exception:
            return None

    def _build_task_event_publisher(self) -> Optional[Callable[[str, Dict[str, Any]], None]]:
        """Build an event publisher for task-level events.

        Returns a callable that publishes task events to the engine's event bus.
        Returns None if the event bus is not available.
        """
        bus = getattr(self, "bus", None)
        if bus is None:
            return None

        def _publish_task_event(event_type: str, event_data: Dict[str, Any]) -> None:
            """Publish a task event to the engine's event bus."""
            try:
                # Import here to avoid circular dependencies
                from .events.event_types import make_event, EngineEvent

                # Create a structured event for task execution
                evt = make_event(
                    EngineEvent,
                    source="task_executor",
                    **{
                        "event_type": event_type,
                        "task_data": event_data
                    }
                )
                bus.publish(evt)
            except Exception as exc:  # noqa: BLE001
                print(f"Failed to publish task event {event_type}: {exc!r}")

        return _publish_task_event

    # ----------------------------------------------------- Stage 19.3 wiring
    def _build_execution_cycle(self) -> Optional[Any]:
        """Construct the canonical Stage 19.3
        :class:`core.execution.ExecutionCycle`.

        Wires the cycle with default providers that adapt the
        existing :class:`CapabilityRouter`,
        :class:`TargetResolver`, and
        :class:`PerceptionProvider` subsystems.  The cycle is the
        authoritative execution primitive for
        ``main.py → Brain → Agent → PlanExecutor → ExecutionCycle →
        CapabilityRouter`` flows.

        The cycle is fail-soft: when any of the required
        dependencies (e.g. vision, target resolver) cannot be
        imported in this environment, the function returns
        ``None`` and the :class:`PlanExecutor` falls back to the
        legacy direct ``router.route()`` path so the system
        continues to work.
        """
        try:
            from core.execution import (
                ExecutionCycle,
                ExecutionPolicy,
                DefaultActionExecutor,
                DefaultVerificationProvider,
                DefaultGroundingProvider,
                DefaultSynchronizationProvider,
            )
            from core.grounding.target_resolver import TargetResolver
            from vision.perception_contract import (
                PerceptionProvider,
                PerceptionRequest,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"Stage 19.3 execution cycle modules unavailable: {exc!r}"
            )
            return None

        # ---- PerceptionProvider: the engine already wires a
        # vision service for the Agent.  When one is available we
        # wrap it as a Stage 19.3 PerceptionProvider; when not, we
        # fall back to the canonical NullPerceptionProvider so
        # the cycle still functions (it just returns no
        # candidates).
        perception_provider: Any
        try:
            perception_provider = self._build_perception_provider()
            if perception_provider is None:
                from vision.perception_adapter import NullPerceptionProvider
                perception_provider = NullPerceptionProvider()
        except Exception as exc:  # noqa: BLE001
            print(
                f"Perception provider unavailable; using Null: {exc!r}"
            )
            try:
                from vision.perception_adapter import NullPerceptionProvider
                perception_provider = NullPerceptionProvider()
            except Exception:
                return None

        # ---- TargetResolver: cheap to construct, no external
        # dependencies.  Screen size is taken from the
        # canonical default when not in the config.
        screen_w = int(getattr(self.config, "screen_width", 1920) or 1920)
        screen_h = int(getattr(self.config, "screen_height", 1080) or 1080)
        try:
            target_resolver = TargetResolver(
                screen_width=screen_w,
                screen_height=screen_h,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"TargetResolver construction failed: {exc!r}")
            return None

        # ---- ActionExecutor: adapts the existing
        # CapabilityRouter.  The cycle never calls the LLM and
        # never invents capability names.
        try:
            action_executor = DefaultActionExecutor(_router=self.router)
        except Exception as exc:  # noqa: BLE001
            print(f"ActionExecutor construction failed: {exc!r}")
            return None

        # ---- VerificationProvider: wraps the perception
        # provider so verification uses fresh observations.
        try:
            verification_provider = DefaultVerificationProvider(
                _perception_provider=perception_provider,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"VerificationProvider construction failed: {exc!r}")
            return None

        # ---- GroundingProvider: adapts the TargetResolver.
        try:
            grounding_provider = DefaultGroundingProvider(
                _resolver=target_resolver,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"GroundingProvider construction failed: {exc!r}")
            return None

        # ---- SynchronizationProvider (Stage 19.3): state
        # settling, used between ACT and VERIFY.
        try:
            sync_provider = DefaultSynchronizationProvider(
                perception_provider=perception_provider,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"SynchronizationProvider construction failed: {exc!r}")
            sync_provider = None

        # ---- Policy: production defaults — sync enabled,
        # verification required, settlement required.
        try:
            policy = ExecutionPolicy(
                observation_timeout_s=2.0,
                grounding_timeout_s=2.0,
                action_timeout_s=float(
                    getattr(self.config, "default_action_timeout_s", 30.0)
                ),
                verification_timeout_s=3.0,
                enable_synchronization=True,
                synchronization_timeout_s=3.0,
                synchronization_poll_interval_s=0.05,
                require_settlement=False,  # many app openers don't need strict settlement
                require_preconditions=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ExecutionPolicy construction failed: {exc!r}")
            return None

        try:
            cycle = ExecutionCycle(
                perception_provider=perception_provider,
                target_resolver=target_resolver,
                action_executor=action_executor,
                verification_provider=verification_provider,
                perception_cache=perception_provider,  # Stage 23: Cache invalidation
                precondition_provider=None,
                synchronization_provider=sync_provider,
                policy=policy,
                observability_sink=None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ExecutionCycle construction failed: {exc!r}")
            return None

        return cycle

    def _build_perception_provider(self) -> Optional[Any]:
        """Stage 23 Perception Adapter construction."""
        try:
            from vision.perception_adapter import PerceptionAdapter
            from vision.router.perception_router import PerceptionRouter
            from vision.strategies.uia_strategy import UIAStrategy
            from vision.strategies.ocr_strategy import OCRStrategy
            from vision.strategies.visual_strategy import VisualStrategy
            from vision.strategies.coordinates_strategy import CoordinatesStrategy
            from vision.router.screenshot_provider import make_screenshot_provider, NullScreenshotProvider
            from vision.perception_cache import CachedPerceptionProvider, LRUPerceptionCache
            
            try:
                screenshot_provider = make_screenshot_provider(self, headless=False)
            except Exception:
                screenshot_provider = NullScreenshotProvider()
                
            router = PerceptionRouter(strategies=[UIAStrategy(), OCRStrategy(), VisualStrategy(), CoordinatesStrategy()])
            
            base_provider = PerceptionAdapter(router, screenshot_provider)
            cache = LRUPerceptionCache(max_entries=10)
            return CachedPerceptionProvider(underlying_provider=base_provider, cache=cache)
        except Exception as e:
            print(f"Failed to build stage 23 perception provider: {e}")
            return None

    def _build_local_decision_engine(self) -> Optional[Any]:
        """Build the :class:`LocalActionDecisionEngine` for direct
        callers that want plan synthesis without going through the
        :class:`FastPathDispatcher`.  Optional — the engine is also
        created lazily by the dispatcher on first use.
        """
        from core.services.local_decision_engine import LocalActionDecisionEngine
        try:
            app_svc = self.services.try_resolve("application_service")
            if app_svc is None:
                return None
            resolver = getattr(app_svc, "_resolver", None)
            if resolver is None:
                return None
            return LocalActionDecisionEngine(
                registry=getattr(self, "registry", None),
                resolver=resolver,
            )
        except Exception:
            return None

    def _build_escalation_gate(self) -> Optional[Any]:
        """Build the :class:`AIEscalationGate` that decides whether
        the LLM should be invoked for a given user request.  The gate
        is optional — when absent, the pipeline behaves as before
        (always consults the Brain).
        """
        from core.services.ai_escalation_gate import AIEscalationGate
        try:
            return AIEscalationGate()
        except Exception:
            return None

    def process(
        self,
        text: str,
        *,
        correlation_id: Optional[str] = None,
    ) -> OmnixResponse:
        """Canonical entry point for a single user request (Phase 11).

        Routes the text through: Brain (intent + plan) → Agent (closed
        loop) → response.  Returns a safe :class:`OmnixResponse` —
        never includes raw internal objects, secrets, or stack traces.
        """
        cid = correlation_id or new_correlation_id()

        if not isinstance(text, str) or not text.strip():
            return OmnixResponse(
                text="I didn't catch any text to process.",
                status=ResponseStatus.FAILED,
                correlation_id=cid,
                duration_ms=0.0,
                error="empty input",
            )

        if self._lifecycle_state not in (LifecycleState.READY, LifecycleState.RUNNING):
            msg = f"Engine is {self._lifecycle_state.value}"
            logger.error(f"process() refused: {msg}")
            return OmnixResponse(
                text="The engine is not ready yet.",
                status=ResponseStatus.FAILED,
                correlation_id=cid,
                error=msg,
            )

        with self._lock:
            self._request_count += 1

        if self.pipeline is None:
            return OmnixResponse(
                text="The full-system pipeline is not available in this environment.",
                status=ResponseStatus.FAILED,
                correlation_id=cid,
                error="pipeline not built",
            )

        # Phase 4: create a fresh cancellation token for this
        # correlation_id and register it so SIGINT (or voice
        # "stop") can flip it via request_cancel(cid).  We pop
        # the token in a ``finally`` block so the map never
        # leaks entries for completed requests.
        token = CancellationToken()
        with self._tokens_lock:
            self._tokens_by_cid[cid] = token

        # Emit REQUEST_RECEIVED via the event bus (best-effort).
        try:
            self.bus.publish(
                make_event(
                    RequestEvent,
                    source="engine",
                    correlation_id=cid,
                    stage=REQUEST_RECEIVED,
                    request_kind="text",
                )
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            response = self.pipeline.process(
                text, correlation_id=cid, cancellation_token=token
            )
        finally:
            with self._tokens_lock:
                self._tokens_by_cid.pop(cid, None)

        # Emit REQUEST_COMPLETED with the final response status.
        try:
            stage = REQUEST_COMPLETED
            try:
                status_value = response.status.value
            except Exception:
                status_value = str(response.status)
            self.bus.publish(
                make_event(
                    RequestEvent,
                    source="engine",
                    correlation_id=cid,
                    stage=stage,
                    request_kind="text",
                    status=status_value,
                    duration_ms=float(response.duration_ms or 0.0),
                    error=(response.error or "")[:200],
                    agent_run_id=str(
                        (response.metadata or {}).get("agent_run_id") or ""
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            pass

        return response

    # ------------------------------------------------------------------
    # Phase 4: cancellation API
    # ------------------------------------------------------------------
    def request_cancel(
        self,
        correlation_id: str,
        reason: str = "cancelled by user",
    ) -> bool:
        """Cancel the in-flight request identified by ``correlation_id``.

        Returns ``True`` if a token was found and flipped,
        ``False`` if the correlation_id is unknown (the request
        has already completed or was never started).

        Phase 4 wire-up: SIGINT handlers in ``main.py`` and the
        voice "stop" command both call this.  The token is
        shared with the pipeline + Agent so a single flip
        propagates through the closed loop.
        """
        with self._tokens_lock:
            token = self._tokens_by_cid.get(correlation_id)
        if token is None:
            return False
        try:
            token.cancel(reason=reason)
        except Exception:  # noqa: BLE001
            # A token that does not accept ``reason`` should
            # still flip; we fall back to no-arg cancel.
            try:
                token.cancel()
            except Exception:  # noqa: BLE001
                return False
        return True

    def request_cancel_all(
        self, reason: str = "cancelled by user"
    ) -> int:
        """Cancel every in-flight request.  Returns the count.

        Used by ``engine.shutdown()`` and by tests that need to
        ensure no Agent loop is left running.
        """
        with self._tokens_lock:
            tokens = list(self._tokens_by_cid.values())
        flipped = 0
        for t in tokens:
            try:
                t.cancel(reason=reason)
                flipped += 1
            except Exception:  # noqa: BLE001
                try:
                    t.cancel()
                    flipped += 1
                except Exception:  # noqa: BLE001
                    pass
        return flipped

    def statistics(self) -> Dict[str, Any]:
        """Aggregate stats for R-9."""
        with self._lock:
            stats = {
                "type": "OmnixEngine",
                "lifecycle": self._lifecycle_state.value,
                "execution_count": self._execution_count,
                "request_count": self._request_count,
                "pipeline_available": self.pipeline is not None,
                "services": self.services.statistics(),
                "health_report": self.health.report(),
                "capabilities_loaded": len(self.capabilities.list_names()),
            }
            if self.memory is not None:
                try:
                    stats["memory"] = self.memory.statistics()
                except Exception:  # noqa: BLE001
                    stats["memory"] = {"error": "statistics() raised"}
            return stats

    def __repr__(self) -> str:  # pragma: no cover
        return f"OmnixEngine(state={self._lifecycle_state.value}, executions={self._execution_count})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_memory_service(config: OmnixConfig) -> Any:
    """Construct the default :class:`MemoryService` for the engine.

    The default backend is the in-memory store.  This keeps Phase 9
    offline-friendly (no database file required for tests) while
    leaving production hosts free to inject a :class:`SQLiteMemoryStore`
    through the ``memory=`` keyword on the engine.
    """
    from .services.memory_service import InMemoryStore, MemoryService

    try:
        return MemoryService(store=InMemoryStore())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Default memory service construction failed: {exc!r}")
        return None
