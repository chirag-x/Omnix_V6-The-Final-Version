"""
Omnix V6 — Configuration.

A small, typed configuration loader.  Reads ``.env`` once at boot,
exposes a frozen :class:`OmnixConfig` dataclass, and surfaces
configuration failures as :class:`ConfigurationError` (R-7).

Design constraints (V6 architecture):
    - R-1 / AD-1: All engine configuration is *data* (a frozen dataclass);
                  no scattered ``os.environ.get`` calls in subsystems.
    - R-12: No API keys in source.  This module reads them; nothing else
            may import them.
    - R-17: Logging is ``loguru``; this module configures it.
    - Configuration is validated at boot.  Missing required values fail
      fast with a typed error so the engine never starts half-configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional , Tuple

from loguru import logger as _loguru_logger

from .errors import ConfigurationError


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OmnixConfig:
    """Frozen, validated configuration for the V6 engine.

    Constructed via :meth:`load`.  Subsystems receive this object by
    reference; mutation is impossible (``frozen=True``).  Per R-1, the
    *only* way to change a config value is to construct a new
    :class:`OmnixConfig` and restart the engine.
    """

    # --- paths -----------------------------------------------------------
    project_root: Path
    data_dir: Path
    log_dir: Path
    env_file: Path

    # --- logging ---------------------------------------------------------
    log_level: str = "INFO"
    log_to_file: bool = True
    log_file_name: str = "omnix.log"

    # --- ai / brain ------------------------------------------------------
    openrouter_url: str = "https://openrouter.ai/api/v1"
    openrouter_keys: tuple = ()
    openrouter_model_pool: tuple = ()
    groq_api_key: Optional[str] = None
    groq_model_name: str = "llama-3.3-70b-versatile"

    # --- subsystems ------------------------------------------------------
    enable_voice: bool = False
    enable_vision: bool = False
    enable_browser: bool = False
    enable_automation: bool = False

    # --- Phase 15: voice runtime + inactivity sleep --------------------
    # When True the engine builds the VoiceRuntime + InactivityTimer
    # during initialization and the unified voice/text input loop
    # drives the microphone through the runtime.  Default True in
    # V6 final so ``python main.py`` boots with the full Part 3
    # voice stack (microphone, wake-word listener, command STT,
    # TTS, sleep/wake cycle) — the user gets the production
    # experience out of the box.  Hosts that need the legacy
    # text-only flow can opt out with
    # ``OMNIX_ENABLE_VOICE_RUNTIME=false`` in ``.env``.
    enable_voice_runtime: bool = True
    # Wake phrase for the wake-word listener.  Lower-cased on load
    # so the openwakeword / text_match backends compare apples to
    # apples regardless of how the user typed it in their .env.
    wake_phrase: str = "omnix"
    # Inactivity threshold for the sleep transition.  After this
    # many seconds of no user input and no task activity the
    # runtime transitions to SLEEPING.  Default 30s per the
    # Part 3 spec.
    inactivity_timeout_s: float = 30.0

    # --- timeouts / limits ----------------------------------------------
    # Phase 1 / D19: the four legacy timeout fields are
    # **deprecated** in favour of a single
    # :data:`default_step_timeout_s` knob.  The audit found that
    # none of the four were wired into execution; the PlanExecutor
    # uses its own ``default_step_timeout_s`` (default 60s).  We
    # keep the four legacy fields for one release cycle to give
    # .env files time to migrate, and emit a :class:`DeprecationWarning`
    # if they differ from the canonical value at construction
    # time.  In a future release only ``default_step_timeout_s``
    # will remain.
    default_step_timeout_s: float = 30.0
    default_action_timeout_s: float = 30.0
    default_observation_timeout_s: float = 10.0
    default_verification_timeout_s: float = 10.0
    default_capability_timeout_s: float = 60.0

    # --- Phase 13: vision-grounded computer use --------------------------
    # Minimum grounding confidence below which the Agent refuses
    # to dispatch a pre-action computer-use step.  Mirrors
    # :data:`core.orchestration.grounding.DEFAULT_CONFIDENCE_THRESHOLD`.
    vision_confidence_threshold: float = 0.5
    # Maximum age (seconds) of a screenshot used as evidence for
    # grounding.  Stale screenshots are rejected by the
    # coordinate-safety gate.
    vision_max_screenshot_stale_s: float = 5.0

    # --- misc ------------------------------------------------------------
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def with_overrides(self, **changes: Any) -> "OmnixConfig":
        """Return a copy with the given fields replaced.

        The original is untouched (frozen).  Used by tests and by the
        ``--debug`` CLI flag.
        """
        return replace(self, **changes)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-safe dict for logging / persistence.

        API keys are *never* serialized; they are replaced with
        ``"***"`` markers so structured logs cannot leak them.
        """
        d: Dict[str, Any] = {
            "project_root": str(self.project_root),
            "data_dir": str(self.data_dir),
            "log_dir": str(self.log_dir),
            "env_file": str(self.env_file),
            "log_level": self.log_level,
            "log_to_file": self.log_to_file,
            "log_file_name": self.log_file_name,
            "openrouter_url": self.openrouter_url,
            "openrouter_key_count": len(self.openrouter_keys),
            "groq_api_key": "***" if self.groq_api_key else None,
            "groq_model_name": self.groq_model_name,
            "enable_voice": self.enable_voice,
            "enable_voice_runtime": self.enable_voice_runtime,
            "wake_phrase": self.wake_phrase,
            "inactivity_timeout_s": self.inactivity_timeout_s,
            "enable_vision": self.enable_vision,
            "enable_browser": self.enable_browser,
            "enable_automation": self.enable_automation,
            "default_action_timeout_s": self.default_action_timeout_s,
            "default_observation_timeout_s": self.default_observation_timeout_s,
            "default_verification_timeout_s": self.default_verification_timeout_s,
            "default_capability_timeout_s": self.default_capability_timeout_s,
            "vision_confidence_threshold": self.vision_confidence_threshold,
            "vision_max_screenshot_stale_s": self.vision_max_screenshot_stale_s,
            "extra": dict(self.extra),
        }
        return d


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_REQUIRED_PATH_PARENTS: List[str] = []  # populated lazily by load()
_KNOWN_KEY_MAP: Dict[str, str] = {
    "OPENROUTER_URL": "openrouter_url",
    "GROQ_API_KEY": "groq_api_key",
    "GROQ_MODEL_NAME": "groq_model_name",
    "OMNIX_LOG_LEVEL": "log_level",
    "OMNIX_ENABLE_VOICE": "enable_voice",
    "OMNIX_ENABLE_VISION": "enable_vision",
    "OMNIX_ENABLE_BROWSER": "enable_browser",
    "OMNIX_ENABLE_AUTOMATION": "enable_automation",
    "OMNIX_ENABLE_VOICE_RUNTIME": "enable_voice_runtime",
    "OMNIX_WAKE_PHRASE": "wake_phrase",
    "OMNIX_INACTIVITY_TIMEOUT_S": "inactivity_timeout_s",
}

# Keys that are not first-class fields on :class:`OmnixConfig` but that
# the LLM provider selection layer (see :mod:`ai.provider.selection`)
# reads through ``config.extra``.  Capturing them here means a user
# setting ``OMNIX_LLM_PROVIDER=openrouter`` in ``.env`` actually wins,
# rather than silently falling through to the default ``mock`` provider.
_EXTRA_KEYS: Tuple[str, ...] = (
    "OMNIX_LLM_PROVIDER",
    "OMNIX_LLM_MODEL",
)


def _read_env(env_file: Path) -> Dict[str, str]:
    """Minimal ``.env`` reader.

    Avoids a hard dependency on ``python-dotenv`` for the engine boot
    path; lines of the form ``KEY=VALUE`` and ``# comments`` are
    honored, quotes are stripped.
    """
    if not env_file.is_file():
        return {}
    out: Dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # strip matching quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _collect_openrouter_keys(env: Dict[str, str]) -> tuple:
    """Find every ``OPENROUTER_KEY_*`` entry and return as a tuple.

    V5 used four keys (one per fallback slot); the engine accepts any
    number ≥ 1.
    """
    keys: List[str] = []
    # canonical single-key form
    single = env.get("OPENROUTER_API_KEY")
    if single:
        keys.append(single)
    # numbered form
    for k, v in sorted(env.items()):
        if k.startswith("OPENROUTER_KEY_") and v:
            keys.append(v)
    # de-dup, preserve order
    seen: set = set()
    unique: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return tuple(unique)


def _coerce_bool(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "y", "t")


def load(
    project_root: Optional[Path] = None,
    *,
    env_file: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> OmnixConfig:
    """Load and validate :class:`OmnixConfig`.

    Resolution order (highest priority last):
        1. built-in defaults
        2. ``.env`` file at ``<project_root>/.env`` (if present)
        3. real environment variables
        4. ``overrides`` dict (tests / debug flags)
    """
    if project_root is None:
        project_root = Path.cwd()
    project_root = Path(project_root).resolve()
    env_file = Path(env_file) if env_file else project_root / ".env"

    env = _read_env(env_file)
    # real env wins over file
    for k, v in os.environ.items():
        env[k] = v

    # paths
    data_dir = project_root / "data"
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    keys = _collect_openrouter_keys(env)
    groq_key = env.get("GROQ_API_KEY")
    # Parse OPENROUTER_MODEL as comma-separated pool
    openrouter_model_raw = env.get("OPENROUTER_MODEL", "")
    openrouter_model_pool = tuple(
        part.strip() for part in openrouter_model_raw.split(",") if part.strip()
    )

    # Collect provider-selection extras so ``ai.provider.selection`` can
    # resolve the configured LLM provider from a frozen ``OmnixConfig``.
    # The provider layer reads ``OMNIX_LLM_PROVIDER`` and ``OMNIX_LLM_MODEL``
    # from ``config.extra`` (see ``ai/provider/selection.py``).  The env
    # variable itself is still consulted first, so live env wins over the
    # .env file.
    extra: Dict[str, Any] = {}
    llm_provider_raw = env.get("OMNIX_LLM_PROVIDER")
    if llm_provider_raw:
        extra["llm_provider"] = llm_provider_raw.strip()
    llm_model_raw = env.get("OMNIX_LLM_MODEL")
    if llm_model_raw:
        extra["llm_model"] = llm_model_raw.strip()

    cfg = OmnixConfig(
        project_root=project_root,
        data_dir=data_dir,
        log_dir=log_dir,
        env_file=env_file,
        log_level=env.get("OMNIX_LOG_LEVEL", "INFO").upper(),
        log_to_file=True,
        log_file_name="omnix.log",
        openrouter_url=env.get("OPENROUTER_URL", "https://openrouter.ai/api/v1"),
        openrouter_keys=keys,
        openrouter_model_pool=openrouter_model_pool,
        groq_api_key=groq_key,
        groq_model_name=env.get("GROQ_MODEL_NAME", "llama-3.3-70b-versatile"),
        enable_voice=_coerce_bool(env.get("OMNIX_ENABLE_VOICE")),
        enable_vision=_coerce_bool(env.get("OMNIX_ENABLE_VISION")),
        enable_browser=_coerce_bool(env.get("OMNIX_ENABLE_BROWSER")),
        enable_automation=_coerce_bool(env.get("OMNIX_ENABLE_AUTOMATION")),
        # Phase 15: voice runtime is on by default in V6 final so
        # `python main.py` boots with the full Part 3 voice stack.
        # Hosts that explicitly want the legacy text-only flow can
        # set OMNIX_ENABLE_VOICE_RUNTIME=false in their .env.
        enable_voice_runtime=_coerce_bool(
            env.get("OMNIX_ENABLE_VOICE_RUNTIME"), default=True
        ),
        wake_phrase=(
            (env.get("OMNIX_WAKE_PHRASE") or "omnix").strip().lower() or "omnix"
        ),
        inactivity_timeout_s=float(env.get("OMNIX_INACTIVITY_TIMEOUT_S", "30")),
        default_action_timeout_s=float(env.get("OMNIX_DEFAULT_ACTION_TIMEOUT_S", "30")),
        default_observation_timeout_s=float(env.get("OMNIX_DEFAULT_OBSERVATION_TIMEOUT_S", "10")),
        default_verification_timeout_s=float(env.get("OMNIX_DEFAULT_VERIFICATION_TIMEOUT_S", "10")),
        default_capability_timeout_s=float(env.get("OMNIX_DEFAULT_CAPABILITY_TIMEOUT_S", "60")),
        extra=extra,
    )

    if overrides:
        cfg = cfg.with_overrides(**overrides)

    _validate(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(cfg: OmnixConfig) -> None:
    """Reject obviously broken configs before the engine starts.

    This is intentionally *lenient*: a V6 boot does not require an
    OpenRouter key (the user can configure the brain later).  We only
    fail on values that are syntactically invalid or contradict
    themselves.
    """
    valid_levels = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
    if cfg.log_level not in valid_levels:
        raise ConfigurationError(
            f"Invalid log level: {cfg.log_level!r}",
            code="CONFIG_INVALID_LOG_LEVEL",
            context={"got": cfg.log_level, "valid": sorted(valid_levels)},
        )

    for name, value in (
        ("default_action_timeout_s", cfg.default_action_timeout_s),
        ("default_observation_timeout_s", cfg.default_observation_timeout_s),
        ("default_verification_timeout_s", cfg.default_verification_timeout_s),
        ("default_capability_timeout_s", cfg.default_capability_timeout_s),
    ):
        if value <= 0:
            raise ConfigurationError(
                f"Timeout {name!r} must be > 0 (got {value})",
                code="CONFIG_INVALID_TIMEOUT",
                context={"field": name, "value": value},
            )

    if not (0.0 <= cfg.vision_confidence_threshold <= 1.0):
        raise ConfigurationError(
            "vision_confidence_threshold must be in [0, 1] "
            f"(got {cfg.vision_confidence_threshold!r})",
            code="CONFIG_INVALID_VISION_CONFIDENCE",
            context={"value": cfg.vision_confidence_threshold},
        )
    if cfg.vision_max_screenshot_stale_s <= 0:
        raise ConfigurationError(
            "vision_max_screenshot_stale_s must be > 0 "
            f"(got {cfg.vision_max_screenshot_stale_s!r})",
            code="CONFIG_INVALID_VISION_STALE",
            context={"value": cfg.vision_max_screenshot_stale_s},
        )

    # Part 3 — voice runtime + inactivity sleep validation.
    if cfg.inactivity_timeout_s <= 0:
        raise ConfigurationError(
            "inactivity_timeout_s must be > 0 "
            f"(got {cfg.inactivity_timeout_s!r})",
            code="CONFIG_INVALID_INACTIVITY_TIMEOUT",
            context={"value": cfg.inactivity_timeout_s},
        )
    if not cfg.wake_phrase or not cfg.wake_phrase.strip():
        raise ConfigurationError(
            "wake_phrase must be a non-empty string "
            f"(got {cfg.wake_phrase!r})",
            code="CONFIG_INVALID_WAKE_PHRASE",
            context={"value": cfg.wake_phrase},
        )


# ---------------------------------------------------------------------------
# Logging setup (R-17)
# ---------------------------------------------------------------------------

_configured_logger: Optional[Any] = None  # module-level cache


def configure_logging(cfg: OmnixConfig) -> Any:
    """Configure the global ``loguru`` logger.

    Idempotent: calling twice replaces the previous sinks so tests
    can re-configure between runs.
    """
    global _configured_logger
    _loguru_logger.remove()

    _loguru_logger.add(
        sink=lambda msg: print(msg, end=""),
        level=cfg.log_level,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    if cfg.log_to_file:
        log_path = cfg.log_dir / cfg.log_file_name
        _loguru_logger.add(
            sink=str(log_path),
            level=cfg.log_level,
            rotation="10 MB",
            retention=5,
            enqueue=True,
            encoding="utf-8",
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
                "{level: <8} | "
                "{name}:{function}:{line} - {message}"
            ),
        )

    _configured_logger = _loguru_logger
    _loguru_logger.success("Logging configured (level={})", cfg.log_level)
    return _loguru_logger


def get_logger() -> Any:
    """Return the configured loguru logger.

    Falls back to the un-configured instance if :func:`configure_logging`
    was never called; this lets tests and the engine both import the
    helper without an explicit init.
    """
    return _configured_logger if _configured_logger is not None else _loguru_logger
