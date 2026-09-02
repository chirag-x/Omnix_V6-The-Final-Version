"""One-shot diagnostic: probe the catalog for a list of app names."""
import sys
def _add_path():
    # Script may run from repo root via relative call; keep cwd first.
    pass
sys.path.insert(0, '.')

import os
os.chdir('.')
os.environ['OMNIX_HEADLESS'] = '1'
os.environ['OMNIX_LLM_PROVIDER'] = 'mock'

from system.application.catalog import ApplicationCatalog
from system.application.resolver import ApplicationResolver
from system.application.discovery import normalize_name

cat = ApplicationCatalog()
cat.initialize()
res = ApplicationResolver(cat)
queries = [
    'chrome', 'notepad', 'calculator', 'vs code', 'code',
    'visual studio code', 'discord', 'paint', 'firefox', 'vlc',
    'file explorer', 'explorer', 'edge', 'msedge', 'powershell',
    'cmd', 'terminal', 'task manager', 'spotify', 'photoshop',
]
print(f'catalog records: {len(cat)}')
print(f'catalog source counts: {cat._stats.source_counts}')
print()
for q in queries:
    r = res.resolve(q)
    if r.is_found and r.record is not None:
        rec = r.record
        ep = (rec.executable_path or '-')[:60]
        print(f'{q:25} OK   name={rec.display_name!r:35} exe={rec.executable!r:25} path={ep!r:60} src={rec.source} conf={rec.confidence}')
    else:
        print(f'{q:25} {r.status:10} reason={r.reason[:60]}')
