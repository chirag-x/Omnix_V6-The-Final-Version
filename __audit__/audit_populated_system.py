"""Phase 10.5 audit: find populated V5 files in system/ that V6 still uses."""
import os
import re

# List of V5-leftover populated files in system/ that may be in use
candidates = []
for root, dirs, files in os.walk('system'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache')):
        continue
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(root, f)
        try:
            sz = os.path.getsize(path)
            if sz > 0:
                candidates.append(path.replace('\\', '/'))
        except OSError:
            pass

# Find which are imported by V6
import_pattern_re = re.compile(r'from\s+([\w\.]+)\s+import|import\s+([\w\.]+)')
all_py = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__', 'system')):
        continue
    for f in files:
        if f.endswith('.py'):
            all_py.append(os.path.join(root, f).replace('\\', '/'))

referenced = {}
for c in candidates:
    modname = c.replace('./', '').replace('/', '.').replace('.py', '')
    refs = set()
    for py in all_py:
        try:
            content = open(py, encoding='utf-8', errors='ignore').read()
        except Exception:
            continue
        for m in import_pattern_re.finditer(content):
            mod = m.group(1) or m.group(2)
            if not mod:
                continue
            if mod == modname or mod.startswith(modname + '.'):
                refs.add(py)
    if refs:
        referenced[c] = refs

print(f'Populated .py files in system/: {len(candidates)}')
print(f'Referenced by V6: {len(referenced)}')
print()
print('=== POPULATED FILES IN system/ REFERENCED BY V6 (KEEP) ===')
for c in sorted(referenced):
    print(f'  {c}: {len(referenced[c])} refs')
print()
print('=== POPULATED FILES IN system/ UNREFERENCED (DELETABLE - V5 legacy) ===')
unref = [c for c in candidates if c not in referenced]
for c in sorted(unref):
    print(f'  {c}')
print()
print(f'TOTAL DELETABLE: {len(unref)}')
