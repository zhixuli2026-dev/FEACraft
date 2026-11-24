"""Verify shared Elmer contact faces between two declared conducting regions."""

import re

from .attempt_workspace import checked_path
from .casedict_schema import interface_regions


def _tag(value):
    if not re.fullmatch(r'[1-9][0-9]{0,8}',str(value)):
        raise ValueError('Elmer electrical interface requires positive native mesh tags')
    return int(value)


def declaration(name, entry):
    settings, regions = entry['elmerElectrical'], interface_regions(entry)
    if (entry.get('type') != 'coupledWall' or entry.get('coupling') != 'electromagnetic'
            or not isinstance(settings,dict) or set(settings) != {'boundaryTag'}
            or not isinstance(regions,list) or len(regions) != 2
            or any(not isinstance(region,str) or not region for region in regions) or regions[0] == regions[1]):
        raise ValueError('Elmer electrical interface needs coupledWall, electromagnetic and two named regions: '+name)
    return {'name':name,'regions':regions,'boundary_tag':_tag(settings['boundaryTag'])}


def declared(case):
    entries = case.get('interfaces')
    if not isinstance(entries,dict):
        return []
    return [declaration(name,entry) for name,entry in sorted(entries.items())
            if isinstance(entry,dict) and 'elmerElectrical' in entry]


def targets(section, name, default):
    settings = [(key,value) for key,value in section.items() if key.startswith(name)]
    if not settings:
        return [default]
    if len(settings) != 1:
        raise ValueError('Elmer electrical interface requires unique native target tags')
    key,value = settings[0]
    size = re.fullmatch(re.escape(name)+r'\(([1-9][0-9]*)\)',key)
    tags = [_tag(item) for item in value.removeprefix('integer ').split()]
    if size is None or len(tags) != int(size[1]) or len(tags) != len(set(tags)):
        raise ValueError('Elmer electrical interface has invalid native target tags')
    return tags


def _faces(root, mesh, tags):
    faces = {tag:[] for tag in tags}
    seen = set()
    with checked_path(root,f'{mesh}/mesh.boundary').open(encoding='ascii') as stream:
        for line in stream:
            if not line.strip():
                continue
            _, tag, left, right, kind, *nodes = map(int,line.split())
            if tag not in faces:
                continue
            points = frozenset(nodes)
            if (min(left,right) <= 0 or left == right or not points or len(points) != kind%100
                    or len(points) != len(nodes) or min(points) <= 0 or points in seen):
                raise ValueError('Elmer electrical interface requires shared interior faces')
            seen.add(points)
            faces[tag].append((left,right,points))
    if any(not group for group in faces.values()):
        raise ValueError('Elmer electrical interface boundary tag has no native faces')
    return faces


def _parents(root, mesh, ids):
    parents = {}
    with checked_path(root,f'{mesh}/mesh.elements').open(encoding='ascii') as stream:
        for line in stream:
            if not line.strip():
                continue
            index, body, _, *nodes = map(int,line.split())
            if index in ids:
                if index in parents or body <= 0:
                    raise ValueError('Elmer electrical interface parent elements lack unique body tags')
                parents[index] = body,frozenset(nodes)
    if set(parents) != ids:
        raise ValueError('Elmer electrical interface references a missing parent element')
    return parents


def bind(root, mesh, case, interfaces, bodies, boundaries):
    tags = {entry['boundary_tag'] for entry in interfaces}
    if len(tags) != len(interfaces):
        raise ValueError('Elmer electrical interfaces require distinct boundary tags')
    if any(tags.intersection(targets(settings,'target boundaries',index)) for index,(_,settings) in boundaries.items()):
        raise ValueError('Elmer electrical interface cannot also have a prescribed boundary condition')
    faces = _faces(root,mesh,tags)
    parents = _parents(root,mesh,{parent for group in faces.values() for left,right,_ in group for parent in (left,right)})
    native_bodies = {}
    for index,(_,settings) in bodies.items():
        for tag in targets(settings,'target bodies',index):
            if tag in native_bodies:
                raise ValueError('Elmer mesh body is assigned to multiple SIF bodies')
            native_bodies[tag] = index
    regions = case.get('regions',{})
    regions = regions if isinstance(regions,dict) else {}
    for interface in interfaces:
        sides = [regions.get(name) for name in interface['regions']]
        if any(not isinstance(side,dict) or side.get('role') != 'electromagneticRegion' for side in sides):
            raise ValueError('Elmer electrical interface requires two declared electromagnetic regions')
        mesh_tags = [_tag(side.get('meshRegion')) for side in sides]
        body_ids = [native_bodies.get(tag) for tag in mesh_tags]
        if None in body_ids or body_ids[0] == body_ids[1]:
            raise ValueError('Elmer electrical interface needs two distinct native SIF bodies')
        group = faces[interface['boundary_tag']]
        for left,right,points in group:
            if {parents[left][0],parents[right][0]} != set(mesh_tags) or not points <= parents[left][1]&parents[right][1]:
                raise ValueError('Elmer electrical interface faces do not connect the declared mesh regions')
        interface.update(mesh_region_tags=mesh_tags,body_ids=body_ids,shared_faces=len(group))
    return interfaces
