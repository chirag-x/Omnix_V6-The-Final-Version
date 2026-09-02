"""
Omnix V6 — Deterministic progress narration.

Maps engine / task / capability events to short, TTS-safe narration
strings.  No LLM roundtrip: every event kind has a fixed template
or, when the source data is meaningful (e.g. the name of an app
being opened), a small formatter that interpolates from the event
payload.

The :func:`narrate` function returns a :class:`SpeechItem` or
``None`` when the event has no user-facing narration.  ``None`` is
*not* an error — many internal events are silent on purpose.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .speech_queue import SpeechItem


# ---------------------------------------------------------------------------
# Capability name → human description
# ---------------------------------------------------------------------------

_CAPABILITY_PHRASES: Dict[str, str] = {
    "desktop.application.open": "I'm opening {target}.",
    "desktop.application.close": "I'm closing {target}.",
    "desktop.application.focus": "I'm focusing {target}.",
    "desktop.application.is_running": "I'm checking if {target} is running.",
    "desktop.keyboard.type": "I'm typing the text.",
    "desktop.keyboard.press": "I'm pressing a key.",
    "desktop.keyboard.hotkey": "I'm pressing a key combination.",
    "desktop.mouse.click": "I'm clicking.",
    "desktop.mouse.double_click": "I'm double-clicking.",
    "desktop.mouse.move": "I'm moving the mouse.",
    "desktop.mouse.scroll": "I'm scrolling.",
    "desktop.mouse.right_click": "I'm right-clicking.",
    "desktop.mouse.drag": "I'm dragging.",
    "desktop.window.list": "I'm listing windows.",
    "desktop.window.focus": "I'm focusing a window.",
    "desktop.window.minimize": "I'm minimizing a window.",
    "desktop.window.maximize": "I'm maximizing a window.",
    "desktop.window.restore": "I'm restoring a window.",
    "desktop.window.close": "I'm closing a window.",
    "browser.search": "I'm searching the web.",
    "browser.navigate": "I'm opening a web page.",
    "browser.click": "I'm clicking on a page.",
    "browser.type": "I'm typing into a field.",
    "browser.open": "I'm opening a browser.",
    "browser.close": "I'm closing the browser.",
    "browser.extract_text": "I'm reading from the page.",
    "browser.scroll": "I'm scrolling the page.",
    "browser.back": "I'm going back.",
    "browser.forward": "I'm going forward.",
    "browser.reload": "I'm reloading the page.",
    "browser.wait": "I'm waiting for the page.",
    "browser.hover": "I'm hovering on an element.",
    "browser.select": "I'm selecting an option.",
    "browser.press": "I'm pressing a key in the browser.",
    "browser.download": "I'm downloading a file.",
    "browser.extract_page": "I'm reading the page content.",
    "filesystem.read": "I'm reading a file.",
    "filesystem.write": "I'm writing a file.",
    "filesystem.create_file": "I'm creating a file.",
    "filesystem.create_folder": "I'm creating a folder.",
    "filesystem.delete": "I'm deleting a file.",
    "filesystem.list": "I'm listing a directory.",
    "process.run": "I'm running a command.",
    "process.is_running": "I'm checking a process.",
    "desktop.observation.screen_size": "I'm checking the screen size.",
    "desktop.observation.foreground_window": "I'm checking the foreground window.",
    "desktop.observation.screenshot": "I'm taking a screenshot.",
}


# ---------------------------------------------------------------------------
# Outcome phrases
# ---------------------------------------------------------------------------

_OUTCOME_PHRASES: Dict[str, str] = {
    "desktop.application.open": "{target} is open.",
    "desktop.application.close": "{target} is closed.",
    "desktop.application.focus": "{target} is focused.",
    "browser.search": "I found the search results.",
    "browser.navigate": "The page is open.",
    "browser.click": "Done.",
    "browser.type": "Typed.",
    "filesystem.write": "File written.",
    "filesystem.create_file": "File created.",
    "filesystem.create_folder": "Folder created.",
    "filesystem.delete": "Deleted.",
    "process.run": "Command finished.",
}


# Failure phrases
_FAILURE_PHRASES: Dict[str, str] = {
    "default": "That didn't work. I'll try another way.",
    "not_found": "I couldn't find that.",
    "permission": "I don't have permission to do that.",
    "timeout": "That took too long.",
}


def _format(template: str, payload: Dict[str, Any]) -> str:
    """Safe ``str.format`` — missing keys are silently dropped so a
    partial event payload does not raise."""
    try:
        return template.format(**{k: v for k, v in payload.items() if isinstance(v, (str, int, float))})
    except Exception:  # noqa: BLE001
        return template


def _capability_payload(event) -> Dict[str, Any]:
    """Extract a target / subject string from a CapabilityEvent."""
    out: Dict[str, Any] = {"capability": event.capability}
    details = event.metadata.get("details") if hasattr(event, "metadata") else None
    if isinstance(details, dict):
        target = details.get("app_name") or details.get("target") or details.get("name")
        if isinstance(target, str) and target:
            out["target"] = target
    if "target" not in out:
        # Use the capability's last segment as a fallback.
        cap = event.capability or ""
        if cap:
            tail = cap.rsplit(".", 1)[-1].replace("_", " ")
            out["target"] = tail
    return out


def _task_payload(event) -> Dict[str, Any]:
    out: Dict[str, Any] = {"step_index": event.step_index, "total_steps": event.total_steps}
    cap = event.metadata.get("capability") if hasattr(event, "metadata") else None
    if isinstance(cap, str):
        out["capability"] = cap
    target = event.metadata.get("target") if hasattr(event, "metadata") else None
    if isinstance(target, str):
        out["target"] = target
    return out


def narrate(event: Any) -> Optional[SpeechItem]:
    """Return a :class:`SpeechItem` for the given event, or ``None``.

    The function is a pure mapping: no I/O, no LLM, no state.  It
    is safe to call from any thread.
    """
    if event is None:
        return None
    name = getattr(event, "name", "") or ""
    # Capability events --------------------------------------------------
    if name == "capability.event":
        transition = getattr(event, "transition", "")
        if transition == "executed":
            cap = getattr(event, "capability", "") or ""
            template = _CAPABILITY_PHRASES.get(cap)
            if template:
                payload = _capability_payload(event)
                return SpeechItem(
                    text=_format(template, payload),
                    priority=50,
                    kind="progress",
                    correlation_id=event.metadata.get("correlation_id", "") if hasattr(event, "metadata") else "",
                )
        if transition == "verified":
            cap = getattr(event, "capability", "") or ""
            template = _OUTCOME_PHRASES.get(cap)
            if template:
                payload = _capability_payload(event)
                return SpeechItem(
                    text=_format(template, payload),
                    priority=80,
                    kind="progress",
                )
        if transition == "failed":
            cap = getattr(event, "capability", "") or ""
            err = (getattr(event, "error", "") or "").lower()
            if "not found" in err or "not_found" in err or "no instances" in err:
                text = _FAILURE_PHRASES["not_found"]
            elif "permission" in err or "access denied" in err:
                text = _FAILURE_PHRASES["permission"]
            elif "timeout" in err:
                text = _FAILURE_PHRASES["timeout"]
            else:
                text = _FAILURE_PHRASES["default"]
            return SpeechItem(text=text, priority=80, kind="progress")
        return None
    # Task events --------------------------------------------------------
    if name == "task.event":
        transition = getattr(event, "transition", "")
        if transition == "step_started":
            payload = _task_payload(event)
            cap = payload.get("capability")
            target = payload.get("target")
            if isinstance(cap, str) and cap in _CAPABILITY_PHRASES:
                tpl = _CAPABILITY_PHRASES[cap]
                if isinstance(target, str) and target and "{target}" in tpl:
                    text = tpl.format(target=target)
                else:
                    text = _format(tpl, payload)
                return SpeechItem(text=text, priority=50, kind="progress")
        if transition == "completed":
            return SpeechItem(text="Done.", priority=80, kind="result")
        if transition == "failed":
            return SpeechItem(
                text="I couldn't complete the task.",
                priority=80,
                kind="failure",
            )
        if transition == "replanning":
            return SpeechItem(
                text="Let me try another way.",
                priority=70,
                kind="progress",
            )
        return None
    # Request lifecycle -------------------------------------------------
    if name == "request.event":
        stage = getattr(event, "stage", "")
        if stage == "completed":
            status = getattr(event, "status", "")
            if status == "ok":
                return SpeechItem(text="Done.", priority=90, kind="result")
            if status in ("failed", "timeout", "rejected"):
                return SpeechItem(
                    text="I couldn't complete the task.",
                    priority=90,
                    kind="failure",
                )
        return None
    return None
