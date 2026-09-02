"""Probe why 'calculator' isn't in the catalog."""
import sys
sys.path.insert(0, '.')

from system.application.catalog import ApplicationCatalog
from system.application.discovery import normalize_name

cat = ApplicationCatalog()
cat.initialize()

# Check if calc.exe is in the catalog
print("Looking for 'calc.exe' in catalog...")
n_calc = normalize_name("calc.exe")
print(f"  normalize('calc.exe') = {n_calc!r}")
n_calcstem = normalize_name("calc")
print(f"  normalize('calc') = {n_calcstem!r}")

# Iterate records
for rec in cat.all_records():
    if rec.executable.lower() in ("calc.exe", "mspaint.exe", "msedge.exe"):
        print(f"  found: {rec.normalized_name!r} -> {rec.executable!r} src={rec.source}")

# Try the targeted lookup
print()
print("Targeted lookup: 'calc'")
rec = cat.lookup("calc")
print(f"  result: {rec}")
if rec:
    print(f"  executable: {rec.executable!r}")

print()
print("Targeted lookup: 'calculator'")
rec = cat.lookup("calculator")
print(f"  result: {rec}")

# Examine records containing 'calc' in any field
print()
print("Records with normalized_name starting with 'calc':")
for rec in cat.all_records():
    if rec.normalized_name.startswith("calc"):
        print(f"  {rec.normalized_name!r:35} exe={rec.executable!r:25} path={(rec.executable_path or '-')[:60]!r:60} src={rec.source}")

# Examine registry Uninstall entries
print()
print("Registry entries with 'calc' or 'paint' or 'edge':")
import winreg
for hive, sub in [
    (0x80000002, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (0x80000002, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
]:
    try:
        with winreg.OpenKey(hive, sub) as parent:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(parent, i)
                    if any(k in name.lower() for k in ("calc", "paint", "edge")):
                        with winreg.OpenKey(parent, name) as ch:
                            try:
                                dn, _ = winreg.QueryValueEx(ch, "DisplayName")
                                print(f"  subkey={name!r} DisplayName={dn!r}")
                            except OSError:
                                pass
                except OSError:
                    break
                i += 1
    except OSError:
        pass

# Start menu
print()
print("Start Menu shortcuts containing 'calc' or 'paint':")
import os
roots = [
    os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
    os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
]
for root in roots:
    if not os.path.isdir(root):
        continue
    for dp, _, files in os.walk(root):
        for f in files:
            low = f.lower()
            if any(k in low for k in ("calc", "paint", "msedge", "edge")):
                print(f"  {os.path.join(dp, f)}")
