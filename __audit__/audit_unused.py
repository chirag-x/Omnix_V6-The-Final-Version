"""Phase 10.5 audit: find all zero-byte/unused files in V6."""
import os
import re
import sys

# Build the list of zero-byte .py files
zero_byte_py = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__')):
        continue
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(root, f)
        try:
            if os.path.getsize(path) == 0:
                zero_byte_py.append(path.replace('\\', '/'))
        except OSError:
            pass

# Search all V6 source + tests for any import of these files
import_pattern_re = re.compile(r'from\s+([\w\.]+)\s+import|import\s+([\w\.]+)')
referenced = {}
all_py_files = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__')):
        continue
    for f in files:
        if not f.endswith('.py'):
            continue
        all_py_files.append(os.path.join(root, f).replace('\\', '/'))

# Pre-compute module name for each zero-byte file
zb_modules = {zf: zf.replace('./', '').replace('/', '.').replace('.py', '') for zf in zero_byte_py}

# For each zero-byte file, find which V6 py files reference it
for zf in zero_byte_py:
    modname = zb_modules[zf]
    refs = set()
    for py in all_py_files:
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
        referenced[zf] = refs

print(f'Total zero-byte .py files: {len(zero_byte_py)}')
print(f'Referenced by V6 code: {len(referenced)}')
print()
print('=== ZERO-BYTE FILES REFERENCED BY V6 (KEEP) ===')
for zf in sorted(referenced):
    print(f'  {zf}')
    for r in sorted(referenced[zf])[:5]:
        print(f'    <- {r}')
print()
print('=== UNREFERENCED ZERO-BYTE FILES (DELETABLE) ===')
unref = [zf for zf in zero_byte_py if zf not in referenced]
for zf in sorted(unref):
    print(f'  {zf}')
print()
print(f'TOTAL DELETABLE: {len(unref)}')
