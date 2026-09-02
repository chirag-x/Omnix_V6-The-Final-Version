"""Probe Notepad record in detail."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from system.application.catalog import ApplicationCatalog
from system.application.resolver import ApplicationResolver

cat = ApplicationCatalog()
cat.initialize()
res = ApplicationResolver(cat)
r = res.resolve("Notepad")
print(f"status={r.status}")
print(f"display_name={r.record.display_name}")
print(f"executable={r.record.executable}")
print(f"executable_path={r.record.executable_path}")
print(f"launch_command={r.record.launch_command}")
print(f"source={r.record.source}")
print(f"metadata={r.record.metadata}")
print(f"process_names={r.record.metadata.get('process_names') if r.record.metadata else None}")
print(f"confidence={r.record.confidence}")
