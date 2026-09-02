"""Probe resolver for known apps."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from system.application.catalog import ApplicationCatalog
from system.application.resolver import ApplicationResolver

cat = ApplicationCatalog()
cat.initialize()
res = ApplicationResolver(cat)
for q in ["Notepad", "Calculator", "Chrome", "Edge", "Spotify", "Discord", "VS Code", "Code", "Word", "Excel", "PowerPoint", "Settings", "Terminal", "Windows Terminal"]:
    r = res.resolve(q)
    if r.record:
        print(f"{q!r:20s} FOUND  src={r.record.source:12s} exe={r.record.executable!r:25s} launch_cmd={r.record.launch_command[:80]!r}")
    elif r.candidates:
        print(f"{q!r:20s} AMBIG ({len(r.candidates)}) -> {[c.executable for c in r.candidates[:3]]}")
    else:
        print(f"{q!r:20s} NOT FOUND")
