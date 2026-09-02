"""Phase 10.5 audit: check populated V5-populated files in mixed dirs."""
import os
import re

# Check populated files in skills/, memory/, automation/, context/, voice/, vision/
mixed_dirs = ['skills', 'memory', 'automation', 'context', 'voice', 'vision',
              'core', 'ai', 'browser', 'utils', 'temp', '__audit__']

# Add 'tests' and root
mixed_dirs += ['tests']

candidates = []
for d in mixed_dirs:
    if not os.path.isdir(d):
        continue
    for root, dirs, files in os.walk(d):
        if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git')):
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

# Also check root-level .py files
for f in os.listdir('.'):
    if f.endswith('.py') and os.path.isfile(f):
        candidates.append(f.replace('\\', '/'))

# Find which are imported by V6 (excluding the file itself)
import_pattern_re = re.compile(r'from\s+([\w\.]+)\s+import|import\s+([\w\.]+)')
all_py = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__')):
        continue
    for f in files:
        if f.endswith('.py'):
            all_py.append(os.path.join(root, f).replace('\\', '/'))

referenced = {}
unref = []
for c in candidates:
    modname = c.replace('./', '').replace('/', '.').replace('.py', '')
    # For root-level files like "test_engine.py", modname is "test_engine"
    if '/' not in c.replace('./', ''):
        modname = c.replace('./', '').replace('.py', '')
    refs = set()
    for py in all_py:
        if py == c:
            continue
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
    else:
        unref.append(c)

print(f'Populated .py files in mixed dirs + root: {len(candidates)}')
print(f'Referenced by V6: {len(referenced)}')
print(f'Unreferenced (potentially deletable): {len(unref)}')
print()
print('=== UNREFERENCED POPULATED FILES ===')
for u in sorted(unref):
    print(f'  {u}')
