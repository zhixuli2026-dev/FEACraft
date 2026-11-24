import json, collections
from pathlib import Path

m = json.loads((Path(__file__).resolve().parents[1] / 'extracted-manifest.json').read_text(encoding='utf-8'))
files = m['files']
locs = m['locations']

def destname(fe):
    return (fe['dest'] or fe['source']).replace('{app}', '').lstrip('\\')

stats = collections.defaultdict(lambda: [0, 0])
for fe in files:
    rel = destname(fe).replace('\\', '/')
    top = rel.split('/')[0]
    li = fe['location_entry']
    size = locs[li]['original_size'] if li >= 0 else 0
    stats[top][0] += 1
    stats[top][1] += size

print('=== top-level dirs (count, MB) ===')
for k, (c, s) in sorted(stats.items(), key=lambda x: -x[1][1]):
    print(f'{k:20s} {c:6d} files  {s/1e6:10.1f} MB')

print()
print('=== largest files ===')
big = []
for fe in files:
    li = fe['location_entry']
    if li < 0:
        continue
    big.append((locs[li]['original_size'], destname(fe)))
for s, n in sorted(big, reverse=True)[:20]:
    print(f'{s/1e6:10.1f} MB  {n}')

print()
print('=== main executables / DLLs in bin root ===')
for fe in files:
    d = destname(fe)
    if d.startswith('bin\\') and d.count('\\') == 1 and d.lower().endswith(('.exe', '.dll')):
        li = fe['location_entry']
        print(f'  {locs[li]["original_size"]/1e6:8.2f} MB  {d}')

print()
print('=== installer metadata ===')
h = m['header']
for k in ['app_name', 'app_ver_name', 'app_id', 'app_version', 'app_publisher',
          'app_publisher_url', 'app_support_url', 'app_updates_url', 'app_copyright',
          'default_dir_name', 'base_filename', 'app_mutex', 'uninstall_display_name']:
    print(f'  {k}: {h.get(k)!r}')
print('  tasks:', m['tasks'])
print('  run entries count:', h['num_run'])
print('  languages:', [(l['name'], l['language_id']) for l in m['languages']])
