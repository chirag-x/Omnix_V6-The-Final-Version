"""Generate list of non-Python files to delete (empty JSONs, root clutter)."""
import os

# Empty JSON config files
empty_jsons = []
for root, dirs, files in os.walk('.'):
    if any(x in root for x in ('.venv', '__pycache__', '.pytest_cache', '.git', '__audit__', '.git')):
        continue
    for f in files:
        if f.endswith('.json'):
            path = os.path.join(root, f).replace(os.sep, '/')
            try:
                if os.path.getsize(path) == 0:
                    empty_jsons.append(path)
            except OSError:
                pass

# Root clutter
root_clutter = []
for f in ['test_engine.py', 'test_memory_api.py', 'patch2.py', 'patch_main.py',
          'runner.py', 'input.txt', 'temp_config_view.txt']:
    if os.path.isfile(f):
        root_clutter.append(f)

# Empty temp dir
temp_dir = 'temp'
if os.path.isdir(temp_dir):
    # include all .py files in temp/ for deletion
    for root, dirs, files in os.walk(temp_dir):
        for f in files:
            if f.endswith('.py'):
                root_clutter.append(os.path.join(root, f).replace(os.sep, '/'))

# Empty assets/ subdirs
empty_dirs = []
for sub in ['assets/animations', 'assets/icons', 'assets/sounds',
            'vision/summary', 'vision/utils', 'voice/contracts',
            'temp/pycache']:
    if os.path.isdir(sub) and not os.listdir(sub):
        empty_dirs.append(sub)

# system/cache and system/config and system/memory zero-byte JSONs are in empty_jsons already

# After deletion, the system subdirs that will be empty:
# system/applications, system/automation, system/browser, system/cache,
# system/config, system/diagnostics, system/filesystem, system/input,
# system/interfaces, system/memory, system/models, system/power,
# system/processes, system/scheduler, system/services, system/utils,
# system/windows (mostly)
# We will only delete the directory if it becomes empty.

print('=== EMPTY JSON FILES TO DELETE ===')
for f in sorted(empty_jsons):
    print(' ', f)
print(f'Count: {len(empty_jsons)}')

print()
print('=== ROOT CLUTTER TO DELETE ===')
for f in sorted(set(root_clutter)):
    print(' ', f)
print(f'Count: {len(set(root_clutter))}')

print()
print('=== EMPTY DIRS TO DELETE ===')
for d in empty_dirs:
    print(' ', d)
print(f'Count: {len(empty_dirs)}')

# Combined list
combined = sorted(set(empty_jsons) | set(root_clutter))
with open('__audit__/delete_nonpy.txt', 'w', encoding='utf-8') as f:
    for fp in combined:
        f.write(fp + '\n')
with open('__audit__/delete_dirs.txt', 'w', encoding='utf-8') as f:
    for d in empty_dirs:
        f.write(d + '\n')

# Also list post-deletion dirs that will be empty
print()
print('=== POST-DELETION EMPTY DIRS (will be cleaned) ===')
dirs_to_check = [
    'automation', 'context', 'memory', 'skills', 'skills/built_in',
    'skills/built_in/applications', 'skills/built_in/browser', 'skills/built_in/files',
    'skills/built_in/input', 'skills/built_in/media', 'skills/built_in/system',
    'skills/built_in/vision', 'skills/core', 'skills/manager', 'skills/capabilities',
    'system/applications', 'system/automation', 'system/browser', 'system/cache',
    'system/config', 'system/diagnostics', 'system/filesystem', 'system/input',
    'system/interfaces', 'system/memory', 'system/models', 'system/power',
    'system/processes', 'system/scheduler', 'system/services', 'system/utils',
    'system/windows', 'utils', 'core/agent', 'core/compatibility', 'core/planning',
    'core/execution',  # core/execution has interfaces.py populated
    'core/state',  # has domain.py + contexts.py + context_service.py populated
    'temp',
    'assets/animations', 'assets/icons', 'assets/sounds',
    'vision/summary', 'vision/utils', 'voice/contracts',
    '__audit__',
]
for d in dirs_to_check:
    if os.path.isdir(d):
        files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))]
        if not files:
            print(f'  (empty after cleanup): {d}')
