"""
Omnix V6 -- Phase 11.6 real-provider probe.

One-shot manual test: build the real OpenRouter provider, fire ONE
LLMRequest against the configured model, print a safe summary, and
exit.  No retries, no spam, no API key printed.

Run from the V6 project root:

    python scripts/phase11_6_real_provider_probe.py

On success, prints:
    provider=<name>  model=<model-id>  elapsed=<s>
    validated=True    kind=<kind>      dialogue=<dialogue-kind>

On failure, prints the typed error code and message; never the
request body or any Authorization header.  Never the API key.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Force headless, quiet boot
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --- safety: never print secrets
_REDACT_NEEDLES = ("sk-", "Bearer ", "bearer ")


def _safe(s: object) -> str:
    if not isinstance(s, str):
        return repr(s)
    for needle in _REDACT_NEEDLES:
        if needle in s:
            return "<redacted>"
    return s


def main() -> int:
    from ai.provider import get_provider
    from ai.provider.contracts import LLMMessage, LLMRequest, MessageRole, OutputFormat
    from core.configuration import load

    cfg = load(ROOT)

    provider = get_provider(cfg)

    sys_prompt = (
        "You are the Omnix V6 Intent Interpreter.  Respond with exactly one "
        "JSON object that conforms to the V6 Intent schema.  No prose, no "
        "markdown, no commentary."
    )
    user_text = "Hello Omnix"
    request = LLMRequest(
        system=sys_prompt,
        messages=[LLMMessage(role=MessageRole.USER, content=user_text)],
        output_format=OutputFormat.JSON,
        temperature=0.0,
        timeout_s=30.0,
    )

    print(f"provider={provider.name}  model={getattr(provider, '_model', '?')!r}")
    t0 = time.time()
    try:
        response = provider.generate(request)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.time() - t0
        code = getattr(exc, "code", "PROVIDER_ERROR")
        msg = getattr(exc, "message", str(exc))
        print(f"  [FAIL] provider_call  elapsed={elapsed:.2f}s  code={code}")
        print(f"  message={_safe(msg)}")
        return 1
    elapsed = time.time() - t0

    # Truncate the response content safely.
    content = response.content or ""
    snippet = content[:300].replace("\n", " ")
    print(f"  [OK ] provider_call  elapsed={elapsed:.2f}s  bytes={len(content)}")
    print(f"  content_snippet={_safe(snippet)!r}")

    # Validate the parsed payload if it parses as JSON.
    import json
    try:
        payload = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] json_parse    error={type(exc).__name__}")
        return 1
    if not isinstance(payload, dict):
        print(f"  [FAIL] json_parse    payload_not_dict")
        return 1

    # Hand to the real validator.
    from ai.intent import build_default_registry, validate_intent_payload
    registry = build_default_registry()
    payload.setdefault("source_text", user_text)
    try:
        intent = validate_intent_payload(payload, registry)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "INTENT_VALIDATION_ERROR")
        msg = getattr(exc, "message", str(exc))
        ctx = getattr(exc, "context", None) or {}
        print(f"  [FAIL] validate_intent  code={code}  message={_safe(msg)}")
        print(f"  context={_safe(repr(ctx))}")
        return 2

    print(
        f"  [OK ] validate_intent  kind={intent.kind.value}  "
        f"dialogue={intent.dialogue_kind.value}  "
        f"confidence={intent.confidence}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
