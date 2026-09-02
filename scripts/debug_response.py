#!/usr/bin/env python3

import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

from main import build_engine

def test_open_notepad():
    print("Building engine...")
    config, engine = build_engine(Path.cwd(), quiet=True, headless=None)

    print("Processing 'Open Notepad.'...")
    response = engine.process("Open Notepad.")

    print(f"Response type: {type(response)}")
    print(f"Response text: {getattr(response, 'text', 'NO TEXT')}")
    print(f"Response status: {getattr(response, 'status', 'NO STATUS')}")
    print(f"Response error: {getattr(response, 'error', 'NO ERROR')}")
    print(f"Response correlation_id: {getattr(response, 'correlation_id', 'NO CID')}")
    print(f"Response duration_ms: {getattr(response, 'duration_ms', 'NO DURATION')}")

    # Check if response has any additional attributes
    attrs = [attr for attr in dir(response) if not attr.startswith('_')]
    print(f"Response attributes: {attrs}")

    return response

if __name__ == "__main__":
    test_open_notepad()