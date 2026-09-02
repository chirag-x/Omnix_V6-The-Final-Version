"""Probe compound split logic only."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from core.services.local_decision_engine import (
    _split_compound,
    _normalise_compound_separators,
    _split_on_top_level_commas,
    _contains_top_level_conjunction,
)

cases = [
    "Open Notepad and type Hello World",
    "Open Notepad, type Hello from Omnix, and save it as omnix_test.txt",
    "Open Notepad, type Hello World",
    "Open Chrome, search for AI agents, and open the second result",
    "save it as omnix_test.txt",
    "save the file as omnix_test.txt",
    "Open Chrome and search for AI agents",
]
for c in cases:
    norm = _normalise_compound_separators(c)
    spl = _split_compound(c)
    print(f"INPUT:    {c!r}")
    print(f"NORM:     {norm!r}")
    print(f"SPLIT:    {spl}")
    print(f"CONTAINS: {_contains_top_level_conjunction(c)}")
    print("---")
