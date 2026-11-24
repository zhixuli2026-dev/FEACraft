"""Named fluid and solid regions for the native OpenFOAM CHT solver."""

from pathlib import PurePosixPath
import re

from .casedict import parse_casedict
from .casedict_schema import interface_pair, interface_regions, valid_cell_zone


SOLVER = 'chtMultiRegionFoam'
MESH_SOURCE = 'importedOpenFOAM'
ROLE_GROUP = {'fluidDomain':'fluid', 'solidPart':'solid'}
INITIAL_FIELDS = {'fluid':('T','U','p','p_rgh'), 'solid':('T',)}


def native_regions(contents):
    raw = parse_casedict(contents).get('regions')
    text = ' '.join(raw) if isinstance(raw, list) else raw
    if not isinstance(text, str) or not text.startswith('(') or not text.endswith(')'):
        raise ValueError('OpenFOAM regionProperties must contain a regions list')
    groups = {}
    pattern = r'(fluid|solid)\s*\(([^()]*)\)'
    body = text[1:-1]
    for group, names in re.findall(pattern, body):
        values = names.split()
        if group in groups or not values or any(not valid_cell_zone(name) for name in values):
            raise ValueError('Invalid OpenFOAM regionProperties group: '+group)
        groups[group] = values
    names = [name for group in groups.values() for name in group]
    if (set(groups) != {'fluid','solid'} or re.sub(pattern, '', body).strip() or len(set(names)) != len(names)
            or 'defaultRegion' in names or 'region0' in names):
        raise ValueError('OpenFOAM regionProperties needs distinct named fluid and solid regions')
    return {name:group for group, keys in groups.items() for name in keys}


def case_regions(case_dictionary, properties):
    expected = native_regions(properties)
    parsed = parse_casedict(case_dictionary)
    regions = parsed.get('regions')
    materials = parsed.get('materials')
    entries = materials.get('entries') if isinstance(materials,dict) else None
    if not isinstance(regions,dict) or not isinstance(entries,dict):
        raise ValueError('CHT caseDict requires declared regions and their materials')
    matched = {}
    for name, region in regions.items():
        if not isinstance(region, dict) or not isinstance(region.get('meshRegion'), str):
            raise ValueError('Every CHT case region requires an explicit meshRegion: '+name)
        native = region['meshRegion']
        role = region.get('role')
        material = region.get('material')
        if (native in matched or not isinstance(role,str) or role not in ROLE_GROUP or expected.get(native) != ROLE_GROUP[role]
                or not isinstance(material,str) or not isinstance(entries.get(material),dict)):
            raise ValueError('CHT meshRegion, role or material conflicts with regionProperties: '+name)
        matched[native] = {'name':name, 'role':role, 'material':material, 'group':expected[native]}
    if set(matched) != set(expected):
        raise ValueError('CHT regionProperties contains regions with no caseDict binding')
    return dict(sorted(matched.items()))


def inspect_inputs(root, run_dir):
    run = root/run_dir
    text = (root/'0-caseDict/caseDict').read_text(encoding='utf8')
    return case_regions(text, (run/'constant/regionProperties').read_text(encoding='utf8'))


def require_initial_fields(root, run_dir, regions, *, allow_generated_materials=False):
    for name, region in regions.items():
        paths = [PurePosixPath('0',name,field) for field in INITIAL_FIELDS[region['group']]]
        if not allow_generated_materials:
            paths.append(PurePosixPath('constant',name,'thermophysicalProperties'))
        for path in paths:
            source = root/run_dir/path
            if not source.is_file() and not source.with_name(source.name+'.gz').is_file():
                raise ValueError('Missing CHT initial field or material dictionary: '+path.as_posix())


def interface_bindings(declarations, regions, patches):
    if not isinstance(declarations,dict):
        return {}
    native_names = {binding['name']:name for name,binding in regions.items()}
    native_patches = {(patch['region'],patch['name']):patch for patch in patches}
    bindings = {}
    for name, entry in declarations.items():
        if (not isinstance(entry,dict) or 'patches' not in entry
                or entry.get('type') not in ('mapped','coupledWall','conformal','nonConformal')):
            continue
        members = interface_regions(entry)
        boundaries = interface_pair(entry['patches'])
        if (not isinstance(members,list) or not isinstance(boundaries,list) or len(members)!=2 or len(boundaries)!=2
                or any(not isinstance(value,str) for value in members+boundaries)):
            raise ValueError('CHT interface requires two named regions and patches: '+name)
        first,second = ((native_names.get(region),patch) for region,patch in zip(members,boundaries))
        left,right = native_patches.get(first),native_patches.get(second)
        if (left is None or right is None or first==second or left.get('sample')!=list(second) or right.get('sample')!=list(first)
                or entry.get('type')=='conformal' and left['nFaces']!=right['nFaces']):
            raise ValueError('CHT interface does not match reciprocal mesh patches: '+name)
        bindings[name] = {'type':entry['type'], 'coupling':entry.get('coupling'),
                          'regions':list(members), 'mesh_patches':[list(first),list(second)]}
    return bindings
