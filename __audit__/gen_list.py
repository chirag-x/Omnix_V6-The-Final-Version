"""Generate full zero-byte deletion list."""
import os
zb = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__')):
        continue
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(root, f).replace(os.sep, '/')
        try:
            if os.path.getsize(path) == 0:
                zb.append(path)
        except OSError:
            pass
zb = sorted(set(zb))
print(f'Total zero-byte .py files (including tests): {len(zb)}')
with open('__audit__/delete_zb2.txt', 'w', encoding='utf-8') as f:
    for fp in zb:
        f.write(fp + '\n')
for fp in zb:
    print(' ', fp)
