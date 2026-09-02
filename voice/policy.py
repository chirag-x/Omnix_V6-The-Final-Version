"""
Voice Policy module.
Handles safety boundaries, response sanitization, and configuration.
"""
from typing import Optional

def sanitize_for_tts(text: str) -> str:
    """Ensure no secrets pass through to TTS engines."""
    if not text:
        return ""
    
    # Strip obvious secrets patterns or refer to environment patterns
    forbidden_tokens = ["api_key", "sk-", "password", "token="]
    lower_text = text.lower()
    
    for token in forbidden_tokens:
        if token in lower_text:
            return "I cannot read this out loud for security reasons."
            
    # Remove markdown artifacts
    cleaned = text.replace("*", "").replace("#", "").replace("_", "")
    return cleaned.strip()

def condense_response(text: str) -> str:
    """Condense raw responses to natural speech constraints."""
    if len(text) > 500:
        return text[:200] + "... I'll stop there to be brief."
    return text
