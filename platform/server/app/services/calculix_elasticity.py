"""Prepare a constant isotropic, small-strain material for a native static CalculiX deck."""

import hashlib
import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import BLOCK, active_material_names, confirmed, scalar
from .case_materials import quantity_si
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


def _card(raw):
    return re.sub(r'\s+','',raw).upper()


def _option(card,name):
    return next((item[len(name)+1:] for item in card.split(',')[1:] if item.startswith(name+'=')),None)


def render(original,selection):
    lines=original.decode('utf-8').splitlines(keepends=True)
    cards={index:_card(line) for index,line in enumerate(lines)
           if line.lstrip().startswith('*') and not line.lstrip().startswith('**')}
    name=selection['native_material']
    matching=[index for index,card in cards.items() if card.startswith('*MATERIAL,') and _option(card,'NAME')==name]
    if len(matching)!=1:
        raise ValueError('Managed elasticity needs one matching *MATERIAL in the main deck')
    header=matching[0]
    end=next((index for index,card in cards.items() if index>header and card.split(',')[0] in
              {'*MATERIAL','*STEP','*SOLIDSECTION','*SHELLSECTION','*BEAMSECTION'}),len(lines))
    local={index:card for index,card in cards.items() if header<index<end}
    if any(card.startswith('*INCLUDE') for card in local.values()):
        raise ValueError('Managed elasticity needs an explicit main-deck material definition')
    nonlinear={'*PLASTIC','*HYPERELASTIC','*HYPERFOAM','*CREEP','*USERMATERIAL','*VISCOELASTIC',
               '*DEFORMATIONPLASTICITY','*DAMAGEINITIATION','*DAMAGEEVOLUTION'}
    if any(card.split(',')[0] in nonlinear for card in local.values()):
        raise ValueError('Native material contains a nonlinear constitutive law')
    elastic=[index for index,card in local.items() if card.split(',')[0]=='*ELASTIC']
    if elastic:
        if len(elastic)!=1 or local[elastic[0]] not in {'*ELASTIC','*ELASTIC,TYPE=ISOTROPIC'}:
            raise ValueError('Managed elasticity needs constant isotropic native *ELASTIC')
        index=elastic[0]
        stop=next((i for i in local if i>index),end)
        data=[line.strip() for line in lines[index+1:stop] if line.strip() and not line.lstrip().startswith('**')]
        values=data[0].split(',') if len(data)==1 else []
        expected=(selection['elastic_modulus_pa'],selection['poisson_ratio'])
        if len(values)!=2 or any(not math.isclose(scalar(value.replace('D','E').replace('d','e')),
                target,rel_tol=1e-12,abs_tol=0) for value,target in zip(values,expected,strict=True)):
            raise ValueError('Native *ELASTIC conflicts with the declared modulus or Poisson ratio')
        return original
    newline='\r\n' if b'\r\n' in original else '\n'
    if not lines[header].endswith(('\n','\r')):
        lines[header]+=newline
    row=f"{selection['elastic_modulus_pa']:.17g},{selection['poisson_ratio']:.17g}"
    lines[header+1:header+1]=['*ELASTIC'+newline,row+newline]
    return ''.join(lines).encode('utf-8')


def prepare(root,source,cards):
    try:
        raw=checked_path(root,CASE_DICT_PATH).read_bytes()
        parsed=parse_casedict(raw.decode('utf-8'))
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    physical=parsed.get('physicalProblem')
    governing=physical.get('governingModels') if isinstance(physical,dict) else None
    model=governing.get('solidMechanics') if isinstance(governing,dict) else None
    if not isinstance(model,dict) or model.get('materialLaw')!='linearElastic':
        return
    active_materials=active_material_names(parsed)
    if len(active_materials)>1:
        raise ValueError('CalculiX activeMaterials needs one material for the declared linearElastic model')
    if 'material' not in model and not active_materials:
        return
    if (str(model.get('enabled','true')).lower() not in {'true','yes'}
            or model.get('kinematics','smallStrain')!='smallStrain' or model.get('dynamics','static')!='static'):
        raise ValueError('Managed CalculiX elasticity needs an enabled small-strain static model')
    material_name=active_materials[0] if active_materials else model['material']
    native=model.get('nativeMaterial',model.get('material',material_name))
    if not isinstance(native,str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,79}',native):
        raise ValueError('Managed CalculiX elasticity needs a native material name')
    native=native.upper()
    controls=[_card(card) for card in cards]
    matching=[card for card in controls if card.startswith('*MATERIAL,') and _option(card,'NAME')==native]
    if len(matching)!=1 or not any(card.startswith('*SOLIDSECTION,') and _option(card,'MATERIAL')==native for card in controls):
        raise ValueError('The native CalculiX material must be assigned to a solid section')
    steps=[card for card in controls if card.split(',')[0]=='*STEP']
    static=[card for card in controls if card.split(',')[0]=='*STATIC']
    if (not steps or len(steps)!=len(static)
            or any('NLGEOM' in step and _option(step,'NLGEOM')!='NO' for step in steps)
            or any(card.split(',')[0] in {'*DYNAMIC','*FREQUENCY','*STEADYSTATEDYNAMICS','*BUCKLE'} for card in controls)):
        raise ValueError('Managed CalculiX elasticity needs linear native static steps')
    materials=parsed.get('materials')
    entries=materials.get('entries') if isinstance(materials,dict) else None
    material=entries.get(material_name) if isinstance(entries,dict) and isinstance(material_name,str) else None
    if not isinstance(material,dict) or not confirmed(material):
        raise ValueError('Managed CalculiX elasticity needs a confirmed named material')
    selection={'material':material_name,'native_material':native,'kinematics':'smallStrain','law':'linearElastic',
               'elastic_modulus_pa':quantity_si('elasticModulus',material.get('elasticModulus')),
               'poisson_ratio':quantity_si('poissonRatio',material.get('poissonRatio'))}
    if active_materials:
        selection['load_case']=parsed[BLOCK]['activeLoadCase']
    source['files'][CASE_DICT_PATH]=record(raw)
    source['elasticity']=selection
    from .calculix_artifacts import render_input
    compiled=render_input(source,checked_path(root,source['input']).read_bytes())
    selection['native_input_sha256']=hashlib.sha256(compiled).hexdigest()
