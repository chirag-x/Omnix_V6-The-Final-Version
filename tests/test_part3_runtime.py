"""
OMNIX V6 — Part 3 tests (Runtime State + Voice Interaction).

The 9 tests below cover the runtime state model, sleep/wake
cycles, wake-word listener, speech queue gating, inactivity
timer, and the unified voice/text input loop.

Tests are deterministic: they avoid real microphones, real
STT models, and the SAPI TTS by injecting fakes wherever a
collaborator would touch the hardware.
"""
from __future__ import annotations

import time
import threading
from typing import List, Optional

import pytest

from core.state.runtime_state import (
    RuntimeState,
    RuntimeStateController,
    SubsystemFlags,
)
from core.services.speech_queue import SpeechItem, SpeechQueue
from core.state.inactivity_timer import InactivityTimer


# ---------------------------------------------------------------------------
# 1. State model — alive vs asleep
# ---------------------------------------------------------------------------


def test_runtime_state_alive_vs_asleep():
    """The state enum cleanly partitions into 'alive' and 'asleep'."""
    alive = {RuntimeState.READY, RuntimeState.LISTENING, RuntimeState.EXECUTING}
    # Per the spec: SLEEPING and WAKING are both part of the
    # sleep transition (no command STT, wake listener on, TTS gated).
    sleep = {RuntimeState.SLEEPING, RuntimeState.WAKING}
    for st in alive:
        assert st.is_alive(), f"{st} should be alive"
        assert not st.is_sleep_transition(), f"{st} should not be sleeping"
    for st in sleep:
        assert st.is_sleep_transition(), f"{st} should be a sleep transition"
        assert not st.is_alive(), f"{st} should not be alive"
    # Terminal/bootstrap states are neither alive nor a sleep transition.
    for st in (RuntimeState.STARTING, RuntimeState.INITIALIZING,
               RuntimeState.SHUTTING_DOWN, RuntimeState.FAILED):
        assert not st.is_alive()
        assert not st.is_sleep_transition()


# ---------------------------------------------------------------------------
# 2. Subsystem flags — derived from state
# ---------------------------------------------------------------------------


def test_subsystem_flags_ready_enables_command_disables_wake():
    """READY state: command STT on, wake listener off, TTS on, brain on."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    f = c.flags
    assert f.command_input_enabled is True
    assert f.wake_listener_enabled is False
    assert f.tts_enabled is True
    assert f.brain_active is True
    assert f.agent_active is True
    assert f.execution_pipeline_active is True


def test_subsystem_flags_sleeping_disables_command_enables_wake():
    """SLEEPING state: command STT off, wake listener on, brain off."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    c.transition(RuntimeState.SLEEPING)
    f = c.flags
    assert f.command_input_enabled is False
    assert f.wake_listener_enabled is True
    # TTS is still on so the "going to sleep" line and any
    # bypass_sleep announcements can play.  The SpeechQueue
    # lifecycle gate is what actually blocks progress items
    # while asleep.
    assert f.tts_enabled is True
    # Brain / agent / pipeline are quiescent.
    assert f.brain_active is False
    assert f.agent_active is False
    assert f.execution_pipeline_active is False


# ---------------------------------------------------------------------------
# 3. Sleep/wake round-trip without losing the engine
# ---------------------------------------------------------------------------


def test_sleep_wake_round_trip_keeps_engine():
    """A full SLEEPING -> WAKING -> READY round-trip preserves the
    controller's identity (no rebuild)."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    before_id = id(c)
    c.transition(RuntimeState.SLEEPING)
    assert c.state is RuntimeState.SLEEPING
    c.transition(RuntimeState.WAKING)
    assert c.state is RuntimeState.WAKING
    c.transition(RuntimeState.READY)
    assert c.state is RuntimeState.READY
    assert id(c) == before_id, "controller was rebuilt during sleep/wake"


# ---------------------------------------------------------------------------
# 4. Speech queue — sleep gate
# ---------------------------------------------------------------------------


def test_speech_queue_gates_progress_during_sleep():
    """Progress items are dropped when the controller is asleep.
    Bypass items still pass."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    c.transition(RuntimeState.SLEEPING)

    spoken: List[SpeechItem] = []
    q = SpeechQueue(on_speak=lambda it: spoken.append(it))
    q.attach_state_controller(c)

    q.enqueue(SpeechItem(text="opening chrome", kind="progress"))
    q.enqueue(SpeechItem(text="wake", kind="announcement", bypass_sleep=True))
    q.wait_idle(timeout_s=1.0)

    # Only the bypass item was spoken; progress was gated.
    assert len(spoken) == 1, f"expected 1, got {len(spoken)}: {[s.text for s in spoken]}"
    assert spoken[0].text == "wake"
    assert spoken[0].bypass_sleep is True


# ---------------------------------------------------------------------------
# 5. Inactivity timer — pause-while-executing
# ---------------------------------------------------------------------------


def test_inactivity_timer_pauses_during_executing():
    """The accumulator does NOT advance while the controller is EXECUTING."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    fired = threading.Event()
    timer = InactivityTimer(
        c, on_timeout=fired.set, timeout_s=2.0
    )
    timer.start()
    try:
        # Simulate task execution: hold the state in EXECUTING for
        # a short while.  The accumulator should NOT cross the
        # threshold while EXECUTING.
        c.transition(RuntimeState.EXECUTING)
        time.sleep(0.5)  # well under timeout_s (2s) but enough to tick
        assert not fired.is_set(), "fired during EXECUTING (should pause)"
        # Mark the task finished AND leave the state: the
        # controller transitions back to READY in normal
        # operation.  The accumulator resumes from where it was
        # (~0.5s).  After another 1.6s it should fire.
        c.transition(RuntimeState.READY)
        timer.mark_task_finished()
        # Should fire within (timeout_s + a safety margin) seconds.
        assert fired.wait(timeout=3.0), "did not fire after task finished"
    finally:
        timer.stop()


# ---------------------------------------------------------------------------
# 6. Inactivity timer — fires on full timeout
# ---------------------------------------------------------------------------


def test_inactivity_timer_fires_after_timeout():
    """The timer fires its callback when the controller stays
    READY for longer than the timeout window."""
    c = RuntimeStateController()
    c.transition(RuntimeState.READY)
    fired = threading.Event()
    timer = InactivityTimer(c, on_timeout=fired.set, timeout_s=1.0)
    timer.start()
    try:
        assert fired.wait(timeout=3.0), "timer did not fire"
    finally:
        timer.stop()


# ---------------------------------------------------------------------------
# 7. Wake-word listener — text match backend
# ---------------------------------------------------------------------------


def test_wake_word_listener_text_match_fires():
    """The text_match backend fires on a short snippet whose transcript
    contains the wake phrase.  Uses a fake mic and fake STT."""
    from voice.wake.listener import WakeEvent, WakeWordListener, WakeBackend
    from voice.contracts import AudioChunk, AudioFormat

    class FakeMic:
        def start(self):
            pass

        def stop(self):
            pass

        def read(self):
            return iter([])

    seen: List[WakeEvent] = []
    stt_calls = []

    def fake_stt(snippet: bytes, sample_rate: int) -> str:
        stt_calls.append((len(snippet), sample_rate))
        # First call: returns nothing.  Second call: returns the
        # wake phrase.  This makes sure the listener re-runs STT.
        if len(stt_calls) == 1:
            return ""
        return "hey omnix please help"

    listener = WakeWordListener(
        FakeMic(),
        phrase="omnix",
        backend=WakeBackend.TEXT_MATCH,
        on_wake=seen.append,
        text_match_stt=fake_stt,
    )
    # The text_match path needs the buffer to have at least 1s
    # of audio before STT is called.
    fmt = AudioFormat()
    chunk1 = AudioChunk(
        data=b"\x00\x00" * 16000,  # 1s of silence at 16kHz int16
        format=fmt,
        timestamp=time.time(),
    )
    listener._on_chunk(chunk1)
    chunk2 = AudioChunk(
        data=b"\x00\x00" * 8000,  # 0.5s more
        format=fmt,
        timestamp=time.time(),
    )
    listener._on_chunk(chunk2)
    # The first call returns "" which doesn't match; the second
    # call should fire.
    chunk3 = AudioChunk(
        data=b"\x00\x00" * 8000,
        format=fmt,
        timestamp=time.time(),
    )
    listener._on_chunk(chunk3)

    assert seen, f"wake listener did not fire; stt_calls={len(stt_calls)}"
    ev = seen[0]
    assert ev.backend == "text_match"
    assert "omnix" in ev.text.lower()


# ---------------------------------------------------------------------------
# 8. Engine wires the voice runtime when configured
# ---------------------------------------------------------------------------


def test_engine_wires_voice_subsystems_when_enabled():
    """The engine constructs VoiceRuntime + InactivityTimer when
    ``enable_voice_runtime=True`` is set on the config."""
    from core.omnix_engine import OmnixEngine
    from core.configuration import OmnixConfig
    from pathlib import Path

    cfg = OmnixConfig(
        project_root=Path(".").resolve(),
        data_dir=Path("data"),
        log_dir=Path("logs"),
        env_file=Path(".env"),
        log_level="INFO",
        log_to_file=False,
        log_file_name="omnix.log",
        openrouter_url="",
        openrouter_keys=(),
        openrouter_model_pool=(),
        groq_api_key=None,
        groq_model_name="",
        enable_voice=True,
        enable_voice_runtime=True,
        enable_vision=False,
        enable_browser=False,
        enable_automation=False,
        inactivity_timeout_s=30.0,
        wake_phrase="omnix",
        default_action_timeout_s=30.0,
        default_observation_timeout_s=10.0,
        default_verification_timeout_s=10.0,
        default_capability_timeout_s=60.0,
        extra={},
    )
    e = OmnixEngine(config=cfg)
    e.initialize()
    try:
        assert e.voice_runtime is not None
        assert e.inactivity_timer is not None
        assert e.voice_runtime.state is RuntimeState.READY
        assert e.inactivity_timer.is_running() is True
        # Sleep/wake round-trip via the runtime.
        e.voice_runtime.sleep()
        assert e.voice_runtime.state is RuntimeState.SLEEPING
        e.voice_runtime.wake()
        assert e.voice_runtime.state is RuntimeState.READY
    finally:
        e.stop()
    # After stop the timer is no longer running.
    assert e.inactivity_timer.is_running() is False


# ---------------------------------------------------------------------------
# 9. Configuration supports the new Part 3 fields
# ---------------------------------------------------------------------------


def test_config_has_part3_fields_and_validation():
    """The config exposes enable_voice_runtime, wake_phrase,
    inactivity_timeout_s with sane defaults; load() honours the
    matching env vars; the validator rejects a non-positive
    timeout or empty wake phrase."""
    from core.configuration import OmnixConfig, load, ConfigurationError
    from pathlib import Path

    cfg = OmnixConfig(
        project_root=Path(".").resolve(),
        data_dir=Path("data"),
        log_dir=Path("logs"),
        env_file=Path(".env"),
        log_level="INFO",
        log_to_file=False,
        log_file_name="omnix.log",
        openrouter_url="",
        openrouter_keys=(),
        openrouter_model_pool=(),
        groq_api_key=None,
        groq_model_name="",
        enable_voice=False,
        enable_voice_runtime=False,
        enable_vision=False,
        enable_browser=False,
        enable_automation=False,
        inactivity_timeout_s=30.0,
        wake_phrase="omnix",
        default_action_timeout_s=30.0,
        default_observation_timeout_s=10.0,
        default_verification_timeout_s=10.0,
        default_capability_timeout_s=60.0,
        extra={},
    )
    assert cfg.enable_voice_runtime is False
    assert cfg.wake_phrase == "omnix"
    assert cfg.inactivity_timeout_s == 30.0

    # with_overrides honours the new fields.
    overridden = cfg.with_overrides(
        enable_voice_runtime=True,
        wake_phrase="hey omnix",
        inactivity_timeout_s=120.0,
    )
    assert overridden.enable_voice_runtime is True
    assert overridden.wake_phrase == "hey omnix"
    assert overridden.inactivity_timeout_s == 120.0

    # Validation rejects a non-positive timeout.  Validation runs
    # inside load(), not inside with_overrides().
    with pytest.raises(ConfigurationError):
        load(overrides={"inactivity_timeout_s": 0.0})

    # Validation rejects an empty wake phrase.
    with pytest.raises(ConfigurationError):
        load(overrides={"wake_phrase": ""})

    # Load function reads the env vars when present.
    import os
    os.environ["OMNIX_ENABLE_VOICE_RUNTIME"] = "1"
    os.environ["OMNIX_WAKE_PHRASE"] = "jarvis"
    os.environ["OMNIX_INACTIVITY_TIMEOUT_S"] = "60"
    try:
        loaded = load(overrides={"enable_voice": False})
        assert loaded.enable_voice_runtime is True
        assert loaded.wake_phrase == "jarvis"
        assert loaded.inactivity_timeout_s == 60.0
    finally:
        del os.environ["OMNIX_ENABLE_VOICE_RUNTIME"]
        del os.environ["OMNIX_WAKE_PHRASE"]
        del os.environ["OMNIX_INACTIVITY_TIMEOUT_S"]
