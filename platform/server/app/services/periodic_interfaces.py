"""Check declared periodic links against native OpenFOAM boundary-pair evidence."""

from .casedict_schema import interface_pair, interface_regions


NATIVE_TYPES = {'cyclic', 'cyclicAMI', 'cyclicACMI'}


def declarations(entries):
    if not isinstance(entries,dict):
        return {}
    return {name:entry for name,entry in entries.items() if isinstance(entry,dict)
            and entry.get('type') in ('periodic','cyclic') and 'patches' in entry}


def verify(entries, regions, patches):
    known_regions = set(regions) if isinstance(regions,dict) else set()
    known_patches = {patch['name']:patch for patch in patches}
    binding = {}
    for name,entry in entries.items():
        members = interface_regions(entry)
        pair = interface_pair(entry['patches'])
        if (not isinstance(members,list) or not isinstance(pair,list) or len(members)!=2 or len(pair)!=2
                or any(not isinstance(value,str) for value in members+pair) or not set(members)<=known_regions):
            raise ValueError('Periodic interface requires defined regions and two named patches: '+name)
        left,right = (known_patches.get(patch) for patch in pair)
        if (left is None or right is None or pair[0]==pair[1] or left['type'] not in NATIVE_TYPES
                or left['type']!=right['type'] or left.get('neighbour')!=pair[1] or right.get('neighbour')!=pair[0]
                or min(left['nFaces'],right['nFaces'])<1 or (left['type']=='cyclic' and left['nFaces']!=right['nFaces'])):
            raise ValueError('Periodic interface does not match reciprocal native mesh patches: '+name)
        binding[name] = {'type':entry['type'], 'coupling':entry.get('coupling'), 'regions':list(members),
                         'patches':list(pair), 'mesh_type':left['type']}
    return binding
