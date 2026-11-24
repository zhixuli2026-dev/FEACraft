"""Build one FloTHERM IR from validated ECXML, PDML and XTXML inputs."""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath

from .flotherm_ecxml import neutral_model
from .flotherm_inputs import MAX_INPUT_BYTES
from .flotherm_ir import build_ir
from .flotherm_pdml import solid_records
from .flotherm_settings import apply_settings
from .flotherm_xtxml import assemblies as xtxml_assemblies


@dataclass(frozen=True)
class Source:
    name: str
    data: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


def read_source(path, *, name=None, limit=MAX_INPUT_BYTES):
    with path.open('rb') as stream:
        data = stream.read(limit+1)
    if len(data) > limit:
        raise ValueError(f'{name or path.name} 超过 {limit//(1024*1024)} MiB')
    return Source(name or path.name, data)


def _group(sources):
    groups = {}
    names = set()
    for item in sources:
        path = PurePosixPath(item.name)
        kind = path.suffix[1:].upper()
        slot = 'XTXML' if kind == 'XTXMLA' else kind
        if slot not in ('ECXML', 'PDML', 'XTXML') or slot in groups:
            raise ValueError('导入需要 ECXML、V2310 PDML 或两者配对，XTXML/XTXMLA 需配套 PDML')
        names.add(path.stem.casefold())
        groups[slot] = item
    if len(names) != 1 or not groups or ('XTXML' in groups and 'PDML' not in groups):
        raise ValueError('FloTHERM 配对文件必须同名，XTXML/XTXMLA 需配套 PDML')
    return groups


def _configuration(source, default):
    if source is None:
        if default is None:
            raise ValueError('独立 PDML 导入需要配置 JSON，提供计算域和环境温度')
        return default, {}
    try:
        settings = json.loads(source.data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('FloTHERM 导入配置必须为有效 JSON') from error
    if not isinstance(settings, dict):
        raise ValueError('FloTHERM 导入配置必须为 JSON 对象')
    domain = settings.pop('domain', None)
    temperature = settings.pop('ambient_temperature_k', None)
    if isinstance(domain, dict):
        unknown = domain.keys()-{'location', 'size', 'ambientTemperature'}
        if unknown:
            raise ValueError(f'不支持的计算域配置项：{sorted(unknown)}')
        domain_location, domain_size = domain.get('location'), domain.get('size')
        if not _numeric(domain_location, 3) or not _numeric(domain_size, 3):
            raise ValueError('计算域位置和尺寸必须各包含三个有限数值')
        temperature = domain.get('ambientTemperature', temperature)
        domain = domain_location+[a+b for a, b in zip(domain_location, domain_size)]
    if default:
        domain = default['domain'] if domain is None else domain
        temperature = default['ambient_temperature_k'] if temperature is None else temperature
    if (not _numeric(domain, 6) or not _numeric([temperature], 1) or temperature <= 0 or
            any(domain[i] >= domain[i+3] for i in range(3))):
        raise ValueError('配置需要有效的 domain 和正开尔文环境温度')
    return {'domain': domain, 'ambient_temperature_k': temperature}, settings


def _numeric(value, count):
    return (isinstance(value, list) and len(value) == count and
            all(type(number) in (int, float) and math.isfinite(number) for number in value))


def _extent(obj):
    shape = obj['shape']
    origin = shape['origin']
    return origin+[a+b for a, b in zip(origin, shape['dimensions'])]


def _matches(left, right):
    from .flotherm_geometry import BOX_UNION, cylinder_matches, dot, plates_match
    from .flotherm_parts import unions_match

    kind = left['shape']['kind']
    if left['type'] == right['type'] == 'twoResistorModel':
        a,b = left['two_resistance'],right['two_resistance']
        if (not math.isclose(dot(a['normal'],b['normal']),1,abs_tol=1e-8) or
                any(not math.isclose(a[key],b[key],rel_tol=1e-8) for key in ('theta_jc_k_w','theta_jb_k_w')) or
                not math.isclose(left['power_w'],right['power_w'],rel_tol=1e-8,abs_tol=1e-12)):
            return False
    shell_companion = (left['type'] == right['type'] == 'enclosure' and kind == BOX_UNION
                       and right['shape']['kind'] == 'box' and right['mapping'] == 'missing-info')
    return ((kind == right['shape']['kind'] or shell_companion) and left['type'] == right['type'] and
            all(math.isclose(a,b,abs_tol=1e-8,rel_tol=1e-8) for a,b in zip(_extent(left),_extent(right))) and
            (kind != 'cylinder' or cylinder_matches(left['shape'],right['shape'])) and
            (kind != 'flat-plate' or plates_match(left['shape'],right['shape'])) and
            (kind != BOX_UNION or shell_companion or unions_match(left['shape'],right['shape'])) and
            ('pcb' not in left or math.isclose(abs(dot(left['pcb']['normal'],right['pcb']['normal'])),1,abs_tol=1e-8)))


def _pair(xml_items, binary_items):
    by_name = {}
    for obj in xml_items:
        by_name.setdefault(obj['name'], []).append(obj)
    linked = set()
    associations = []
    for obj in binary_items:
        choices = [candidate for candidate in by_name.get(obj['name'], ()) if _matches(candidate, obj)]
        if len(choices) != 1 or choices[0]['id'] in linked:
            raise ValueError(f'ECXML/PDML 的对象 {obj["path"]} 缺少唯一的名称和包围盒配对')
        match = choices[0]
        linked.add(match['id'])
        match['pdml_record_offset'] = obj['record_offset']
        criteria = ['name','bounds_m']
        if obj['shape']['kind'] == 'cylinder':
            criteria.extend(('axis','cross_section'))
        if obj['shape']['kind'] == 'flat-plate':
            criteria.append('plane_corners')
        if 'pcb' in obj:
            criteria.append('board_normal')
        if 'two_resistance' in obj:
            criteria.extend(('normal','resistance','power_w'))
        if 'heatsink' in obj:
            criteria.append('part_corners')
        associations.append({'object':match['id'], 'pdml_offset':obj['record_offset'], 'verified_by':criteria})
    if len(linked) != len(xml_items):
        raise ValueError('ECXML 与 PDML 的活动实体数量不同，无法核验为同一模型')
    return associations


def import_sources(sources, configuration=None):
    from .flotherm_observations import bind_probes, pair_probes
    from .flotherm_ir import validate_ir

    groups = _group(sources)
    ecxml = groups.get('ECXML')
    pdml = groups.get('PDML')
    xtxml = groups.get('XTXML')
    model = neutral_model(ecxml.data, ecxml.name) if ecxml else None
    conditions, overrides = _configuration(configuration, model['source_conditions'] if model else None)
    if pdml:
        objects, materials, assemblies, monitors, regions = solid_records(
            pdml.data, pdml.name, use_binary_material=model is None)
        if model is None:
            model = build_ir(pdml.name, pdml.data, objects, materials, monitors=bind_probes(monitors, objects))
            model['source']['format'] = 'PDML'
            model['assembly']['groups'] = assemblies
            if regions:
                model['regions'] = regions
        else:
            model['source_associations'] = _pair(model['objects'], objects)
            model['assembly']['pdml_groups'] = assemblies
            model['monitors'] = bind_probes(pair_probes(model['monitors'], monitors), model['objects'])
            model['regions'].extend(regions)
        if xtxml:
            by_offset = {obj['record_offset']: obj['id'] for obj in objects}
            if ecxml:
                by_offset = {row['pdml_offset']: row['object'] for row in model['source_associations']}
            geometry = [{**obj, 'id': by_offset[obj['record_offset']]} for obj in objects]
            model['assembly']['xtxml'] = xtxml_assemblies(xtxml.data, geometry)
    domain = conditions['domain']
    for obj in model['objects']:
        limits = _extent(obj)
        if any((limits[i] < domain[i] and not math.isclose(limits[i], domain[i], abs_tol=1e-9)) or
               (limits[i+3] > domain[i+3] and not math.isclose(limits[i+3], domain[i+3], abs_tol=1e-9)) for i in range(3)):
            raise ValueError(f'对象 {obj["path"]} 超出配置计算域')
    model['source_conditions'] = conditions
    model['source']['inputs'] = [{'path': part.name, 'sha256': part.sha256} for part in sources]
    if configuration is not None:
        if overrides:
            model = apply_settings(model, overrides)
        model['source']['configuration'] = {'path': configuration.name, 'sha256': configuration.sha256}
    return validate_ir(model)
