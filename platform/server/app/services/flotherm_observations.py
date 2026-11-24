"""Preserve source probe locations and region coordinates in the thermal IR."""
import math

from .flotherm_geometry import point_relation, volume


def probe(identifier, name, path, position, source):
    return {'id': identifier, 'type': 'probe', 'name': name, 'path': path,
            'locations': [list(position)], 'objects': [], 'sources': [source]}


def region(identifier, kind, name, path, low, high, source):
    return {'id': identifier, 'type': kind, 'name': name, 'path': path,
            'bounds_m': list(low)+list(high), 'source': source}


def bind_probes(monitors, objects):
    for monitor in monitors:
        point = monitor['locations'][0]
        monitor['objects'] = []
        monitor.pop('reason', None)
        candidates = []
        for obj in objects:
            if 'flow_device' in obj:
                continue
            shape = obj['shape']
            if point_relation(shape,point) == 1:
                candidates.append((volume(shape),obj['id']))
        candidates.sort()
        if not candidates:
            monitor['reason'] = '探针没有严格位于已映射固体的内部'
        elif len(candidates) > 1 and math.isclose(candidates[0][0], candidates[1][0], rel_tol=1e-9):
            monitor['reason'] = '探针位于多个体积相同的固体内部，归属不唯一'
        else:
            monitor['objects'] = [candidates[0][1]]
    return monitors


def pair_probes(primary, companion):
    by_name = {}
    for monitor in primary:
        by_name.setdefault(monitor['name'], []).append(monitor)
    linked = set()
    for monitor in companion:
        siblings = by_name.get(monitor['name'])
        if not siblings:
            primary.append(monitor)
            continue
        matches = [row for row in siblings if all(math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-9)
                   for a, b in zip(row['locations'][0], monitor['locations'][0]))]
        if len(matches) != 1 or matches[0]['id'] in linked:
            raise ValueError(f'ECXML/PDML 的同名探针坐标无法唯一配对：{monitor["path"]}')
        match = matches[0]
        linked.add(match['id'])
        match['sources'].extend(monitor['sources'])
    return primary
