"""Compile named mechanical interfaces into a private CalculiX input deck."""

import hashlib
import math
import re

from .attempt_workspace import checked_path
from .casedict import CASE_DICT_PATH, parse_casedict
from .casedict_schema import interface_pair
from .compose_artifacts import record


_SURFACE = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,39}\Z')
_STEP = re.compile(rb'^\s*\*STEP(?=[,\r\n]|$)', re.IGNORECASE | re.MULTILINE)
_OWNED_CONTACT_CARDS = {'*CONTACTPAIR', '*SURFACEINTERACTION'}


def declaration(name, interface):
    law = interface['contactLaw']
    message = f'interfaces.{name}.contactLaw'
    if interface.get('type') != 'contact' or interface.get('coupling') != 'mechanical':
        raise ValueError(message + ' 需要机械接触接口')
    if (not isinstance(law,dict) or set(law) != {'model','normalStiffness','unit'}
            or law['model'] != 'linearPenalty' or str(law['unit']).strip('"') != 'Pa/m'):
        raise ValueError(message + ' 需要 linearPenalty、正法向刚度及 Pa/m 单位')
    try:
        stiffness = float(law['normalStiffness'])
    except (TypeError,ValueError):
        raise ValueError(message + ' 法向刚度必须是正数') from None
    if not math.isfinite(stiffness) or stiffness <= 0:
        raise ValueError(message + ' 法向刚度必须是有限正数')
    sides = interface_pair(interface.get('patches'))
    if (not isinstance(sides,list) or len(sides) != 2
            or any(not isinstance(side,str) or not _SURFACE.fullmatch(side) for side in sides)
            or sides[0].casefold() == sides[1].casefold()):
        raise ValueError(message + ' 必须指定两个不同的 CalculiX 单元面')
    return {'name':name,'dependent_surface':sides[0].upper(),
            'independent_surface':sides[1].upper(),'normal_stiffness_pa_per_m':stiffness}


def render(original, interfaces):
    position = _STEP.search(original)
    if position is None:
        raise ValueError('托管 CalculiX 接触需要在主输入中声明求解步骤')
    keywords = []
    for number, interface in enumerate(interfaces,1):
        law = f'VFCONTACT{number}'
        keywords.extend((
            '*SURFACE INTERACTION,NAME='+law,
            '*SURFACE BEHAVIOR,PRESSURE-OVERCLOSURE=LINEAR',
            format(interface['normal_stiffness_pa_per_m'],'.16g'),
            '*CONTACT PAIR,TYPE=SURFACE TO SURFACE,INTERACTION='+law,
            interface['dependent_surface']+','+interface['independent_surface'],
        ))
    return original[:position.start()] + ('\n'.join(keywords)+'\n').encode('ascii') + original[position.start():]


def prepare(root, source, cards):
    try:
        raw = checked_path(root,CASE_DICT_PATH).read_bytes()
    except FileNotFoundError:
        return
    try:
        parsed = parse_casedict(raw.decode('utf-8'))
    except (UnicodeDecodeError,ValueError):
        return  # Existing native-only inputs may lack a usable platform dictionary.
    group = parsed.get('interfaces')
    if not isinstance(group,dict):
        return
    interfaces = [declaration(name,interface) for name,interface in sorted(group.items())
                  if isinstance(interface,dict) and 'contactLaw' in interface]
    if not interfaces:
        return
    units = parsed.get('caseMeta',{}).get('units',{})
    if not isinstance(units,dict) or str(units.get('length','m')).strip('"') != 'm':
        raise ValueError('托管 CalculiX 接触需要以 m 和 N 编写原生输入')
    labels = set()
    for line in cards:
        keyword,*clauses = line.upper().split(',')
        if keyword in _OWNED_CONTACT_CARDS:
            raise ValueError('托管接触声明与原生接触卡重复')
        if keyword == '*SURFACE' and not any(item == 'TYPE=NODE' for item in clauses):
            labels.update(item[5:] for item in clauses if item.startswith('NAME='))
    pairs = set()
    for interface in interfaces:
        pair = frozenset((interface['dependent_surface'],interface['independent_surface']))
        if not pair <= labels:
            raise ValueError('CalculiX 输入缺少声明的单元面：'+', '.join(sorted(pair-labels)))
        if pair in pairs:
            raise ValueError('CalculiX 接触对重复')
        pairs.add(pair)
    main = checked_path(root,source['input']).read_bytes()
    if sum(line.upper().split(',')[0] == '*STEP' for line in cards) != len(_STEP.findall(main)):
        raise ValueError('托管 CalculiX 接触的求解步骤须在主输入中声明')
    native = render(main,interfaces)
    source['files'][CASE_DICT_PATH] = record(raw)
    source['contact'] = {'interfaces':interfaces,'native_input_sha256':hashlib.sha256(native).hexdigest()}
