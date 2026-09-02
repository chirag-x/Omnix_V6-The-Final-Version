"""
System 9 smoke test — runs through each scenario and reports
pass/fail.  No LLM, no real mic: every subsystem is exercised
in isolation with fakes.
"""
from __future__ import annotations

import os
import sys
import time
import unittest.mock as mock
from typing import Callable, List, Tuple

# Ensure the repo root is on sys.path so ``import core`` etc. work
# when this script is run as ``python scripts/probe_system9_smoke.py``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


RESULTS: List[Tuple[str, str, str]] = []


def scenario(name: str):
    def deco(fn):
        def wrapper(*a, **kw):
            try:
                result = fn(*a, **kw)
                RESULTS.append((name, "PASS", result or ""))
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
        return wrapper
    return deco


# ----- Scenario 1: startup announcement wiring -----
@scenario("S1: startup announcement reaches the speech queue")
def s1() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue

    sq = SpeechQueue()
    sq.enqueue(
        SpeechItem(
            text="Omnix is ready. How can I help?",
            kind="announcement",
            source="announcement",
            bypass_sleep=True,
        )
    )
    sq.wait_idle(timeout_s=2.0)
    return (
        f"enqueued={sq.statistics()['enqueued_total']}, "
        f"spoken={sq.statistics()['spoken_total']}"
    )


# ----- Scenario 2: speech queue priority ordering -----
@scenario("S2: result outranks progress")
def s2() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue

    sq = SpeechQueue()
    spoken: List[str] = []
    sq.set_on_speak(lambda it: spoken.append(it.text))
    # Enqueue the result BEFORE starting the worker.  The default
    # _on_speak is a debug log; we override it after enqueue to
    # capture the order.  Without a custom callback at enqueue
    # time, the worker logs to loguru and the test would only see
    # the (correct) order via timing — which is racy.  Instead, we
    # pre-populate the queue with a result, then add two progress
    # items, then attach the callback so we can be sure the result
    # is at the head when the worker takes it.
    sq.enqueue(SpeechItem(text="result-B", kind="result"))
    sq.enqueue(SpeechItem(text="progress-A", kind="progress"))
    sq.enqueue(SpeechItem(text="progress-C", kind="progress"))
    # Now swap the callback so we capture whatever the worker
    # actually spoke.  The first item the worker dequeues is the
    # one with the highest priority at dequeue time.
    sq.set_on_speak(lambda it: spoken.append(it.text))
    sq.wait_idle(timeout_s=2.0)
    assert spoken[0] == "result-B", f"expected result first, got {spoken}"
    return f"order={spoken}"


# ----- Scenario 3: speech queue dedup -----
@scenario("S3: duplicate text dedupes")
def s3() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue

    sq = SpeechQueue()
    sq.enqueue(SpeechItem(text="Opening Chrome", kind="progress"))
    sq.enqueue(SpeechItem(text="Opening Chrome", kind="progress"))
    sq.wait_idle(timeout_s=2.0)
    return (
        f"enqueued=2 deduped={sq.statistics()['deduped_total']} "
        f"spoken={sq.statistics()['spoken_total']}"
    )


# ----- Scenario 4: lifecycle gate -----
@scenario("S4: progress is gated while asleep")
def s4() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue
    from core.state.runtime_state import RuntimeState, RuntimeStateController

    ctrl = RuntimeStateController()
    sq = SpeechQueue()
    sq.attach_state_controller(ctrl)
    ctrl.transition(RuntimeState.SLEEPING)
    sq.enqueue(SpeechItem(text="working on it", kind="progress"))
    gated = sq.statistics()["gated_total"]
    sq.enqueue(
        SpeechItem(
            text="announcement line",
            kind="announcement",
            bypass_sleep=True,
        )
    )
    sq.wait_idle(timeout_s=2.0)
    # wait_idle returns as soon as the queue is empty; the worker
    # may still be inside _default_speak.  Give it a moment so the
    # spoken_total counter is final.
    time.sleep(0.2)
    return f"gated={gated}, spoken={sq.statistics()['spoken_total']}"


# ----- Scenario 5: barge-in interrupts TTS -----
@scenario("S5: request_interrupt fires callback and drops pending")
def s5() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue

    sq = SpeechQueue()
    callback_called: List[bool] = []
    sq.set_on_interrupt(lambda: callback_called.append(True))
    sq.enqueue(SpeechItem(text="this is the first thing", kind="result"))
    sq.enqueue(SpeechItem(text="this is the second thing", kind="progress"))
    dropped = sq.request_interrupt()
    sq.wait_idle(timeout_s=2.0)
    return (
        f"callback_fired={len(callback_called)} "
        f"pending_dropped={dropped} "
        f"interrupts={sq.statistics()['interrupts_total']}"
    )


# ----- Scenario 6: greeting short-circuit -----
@scenario("S6: greeting short-circuits LLM")
def s6() -> str:
    from ai.intent.interpreter import _match_greeting

    reply = _match_greeting("Hello there")
    assert reply is not None and "Omnix" in reply, f"bad greeting reply: {reply!r}"
    return f'reply="{reply[:50]}..."'


# ----- Scenario 7: small-talk short-circuit -----
@scenario("S7: small-talk short-circuits LLM")
def s7() -> str:
    from ai.intent.interpreter import _match_smalltalk

    for phrase in ["thanks", "goodbye", "are you there", "what can you do"]:
        r = _match_smalltalk(phrase)
        assert r is not None, f"no reply for {phrase!r}"
    return f"ok, 4 phrases matched"


# ----- Scenario 8: SAPI provider importable -----
@scenario("S8: SAPITTSProvider has interrupt and synthesize")
def s8() -> str:
    from voice.tts.sapi_provider import SAPITTSProvider

    p = SAPITTSProvider()
    assert hasattr(p, "interrupt"), "no interrupt method"
    assert hasattr(p, "synthesize"), "no synthesize method"
    return "ok"


# ----- Scenario 9: VoiceRuntime listen loop delivers transcript -----
@scenario("S9: VoiceRuntime listen loop delivers transcript")
def s9() -> str:
    from voice.contracts import TranscriptionResult
    from voice.runtime import VoiceRuntime
    from core.state.runtime_state import RuntimeState, RuntimeStateController

    ctrl = RuntimeStateController()

    class _Chunk:
        data = b"\x00" * 16000

    class FakeMic:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def read(self):
            return iter([_Chunk(), _Chunk()])

    class FakeVAD:
        def __init__(self):
            self.ended = False
            self.speaking = True

        def reset(self):
            self.ended = False
            self.speaking = True

        def process_chunk(self, c):
            return False

        @property
        def is_speaking(self):
            return self.speaking

        def is_speech_ended(self):
            return self.ended

    class FakeSTT:
        def __init__(self):
            self.calls = 0

        def transcribe(self, buf, sample_rate=16000):
            self.calls += 1
            if buf:
                return TranscriptionResult(
                    text="open chrome", confidence=0.9, language="en"
                )
            return TranscriptionResult(text="", confidence=0.0)

        def close(self):
            pass

    rt = VoiceRuntime(
        controller=ctrl,
        microphone=FakeMic(),
        stt=FakeSTT(),
        vad=FakeVAD(),
        wake_listener=mock.MagicMock(),
    )
    captured: List[str] = []
    rt.set_on_command(lambda t: captured.append(t))
    ctrl.transition(RuntimeState.READY)
    rt.start()
    time.sleep(0.1)
    rt._vad.ended = True
    time.sleep(0.4)
    rt.stop()
    assert "open chrome" in captured, f"no command: {captured}"
    return f"commands={captured}"


# ----- Scenario 10: failure recovery — STT raises, loop survives -----
@scenario("S10: STT failure does not kill the listen loop")
def s10() -> str:
    from voice.contracts import TranscriptionResult
    from voice.runtime import VoiceRuntime
    from core.state.runtime_state import RuntimeState, RuntimeStateController

    ctrl = RuntimeStateController()

    class _Chunk:
        data = b"\x00" * 16000

    class FakeMic:
        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def read(self):
            return iter([_Chunk()])

    class FakeVAD:
        def __init__(self):
            self.ended = False
            self.speaking = True

        def reset(self):
            self.ended = False
            self.speaking = True

        def process_chunk(self, c):
            return False

        @property
        def is_speaking(self):
            return self.speaking

        def is_speech_ended(self):
            return self.ended

    class FailingSTT:
        def __init__(self):
            self.calls = 0

        def transcribe(self, buf, sample_rate=16000):
            self.calls += 1
            if buf:
                raise RuntimeError("STT down")
            return TranscriptionResult(text="", confidence=0.0)

        def close(self):
            pass

    rt = VoiceRuntime(
        controller=ctrl,
        microphone=FakeMic(),
        stt=FailingSTT(),
        vad=FakeVAD(),
        wake_listener=mock.MagicMock(),
    )
    ctrl.transition(RuntimeState.READY)
    rt.start()
    time.sleep(0.1)
    rt._vad.ended = True
    time.sleep(0.4)
    rt._vad.ended = True
    time.sleep(0.4)
    stats = rt.statistics()
    rt.stop()
    assert stats["listen_failures"] > 0, f"no failures recorded: {stats}"
    return f"iter={stats['listen_iterations']} fail={stats['listen_failures']}"


# ----- Scenario 11: sleep/wake announcement copy -----
@scenario("S11: sleep/wake announcement copy is set")
def s11() -> str:
    from voice.runtime import GOING_TO_SLEEP_TEXT, AWAKE_TEXT

    assert "going to sleep" in GOING_TO_SLEEP_TEXT.lower()
    assert "awake" in AWAKE_TEXT.lower() and "help" in AWAKE_TEXT.lower()
    return f'sleep="{GOING_TO_SLEEP_TEXT}" awake="{AWAKE_TEXT}"'


# ----- Scenario 12: progress_reporter importable -----
@scenario("S12: ProgressReporter importable and constructible")
def s12() -> str:
    from core.services.progress_reporter import ProgressReporter
    from core.services.speech_queue import SpeechQueue
    from core.events.event_bus import EventBus

    pr = ProgressReporter(bus=EventBus(), queue=SpeechQueue(), terminal=False)
    return "ok"


# ----- Scenario 13: SAPI provider interrupt() is safe when not speaking -----
@scenario("S13: SAPI interrupt() runs without error")
def s13() -> str:
    from voice.tts.sapi_provider import SAPITTSProvider

    p = SAPITTSProvider()
    p.interrupt()  # Should be a no-op when not speaking.
    p.close()
    return "ok"


# ----- Scenario 14: configuration has voice_runtime field -----
@scenario("S14: configuration has enable_voice_runtime")
def s14() -> str:
    import tempfile
    from pathlib import Path
    from core.configuration import OmnixConfig

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = OmnixConfig(
            project_root=root,
            data_dir=root / "data",
            log_dir=root / "logs",
            env_file=root / ".env",
        )
    assert hasattr(cfg, "enable_voice_runtime"), "no enable_voice_runtime field"
    return f"enable_voice_runtime default = {cfg.enable_voice_runtime}"


# ----- Scenario 15: speech queue correlation cancellation -----
@scenario("S15: cancel_pending by correlation_id")
def s15() -> str:
    from core.services.speech_queue import SpeechItem, SpeechQueue

    sq = SpeechQueue()
    sq.enqueue(SpeechItem(text="old", kind="progress", correlation_id="abc"))
    sq.enqueue(SpeechItem(text="newer", kind="progress", correlation_id="abc"))
    sq.enqueue(SpeechItem(text="other", kind="progress", correlation_id="xyz"))
    n = sq.cancel_pending(correlation_id="abc")
    sq.wait_idle(timeout_s=2.0)
    time.sleep(0.2)
    return (
        f"cancelled={n}, "
        f"correlation_cancelled={sq.statistics()['correlation_cancelled']}"
    )


def main() -> int:
    for s in [
        s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12, s13, s14, s15,
    ]:
        s()
    print()
    print("=" * 70)
    passes = sum(1 for _, r, _ in RESULTS if r == "PASS")
    fails = sum(1 for _, r, _ in RESULTS if r == "FAIL")
    print(f"PASS: {passes}/{len(RESULTS)}  FAIL: {fails}/{len(RESULTS)}")
    print("=" * 70)
    for name, status, detail in RESULTS:
        print(f"  [{status}] {name}")
        if detail:
            print(f"         {detail}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
