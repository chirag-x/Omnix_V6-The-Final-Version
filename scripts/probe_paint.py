"""Probe resolver for Paint."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from system.application.catalog import ApplicationCatalog
from system.application.resolver import ApplicationResolver

cat = ApplicationCatalog()
cat.initialize()
res = ApplicationResolver(cat)
r = res.resolve("Paint")
print(f"status={r.status}")
print(f"reason={r.reason}")
print(f"record={r.record}")
print(f"candidates={r.candidates}")
print()
# Try variations
for q in ["Paint", "paint", "MS Paint", "Microsoft Paint", "mspaint", "Paint.NET", "paint.net"]:
    r = res.resolve(q)
    print(f"{q!r:20s} -> {r.status}  rec={r.record.executable if r.record else None}")
