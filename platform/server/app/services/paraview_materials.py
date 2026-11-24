"""Resolve editable OSPRay material libraries against a sealed case."""

import json
import math
from pathlib import PurePosixPath
import re

from .attempt_workspace import canonical_path, checked_path


NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
TYPES = {'Principled', 'OBJMaterial', 'Glass', 'ThinGlass', 'Metal', 'Alloy', 'CarPaint',
         'MetallicPaint', 'Plastic', 'Luminous'}
TEXTURES = {'.png','.jpg','.jpeg','.bmp','.pnm','.ppm','.tif','.tiff','.vti','.hdr'}


def valid_name(value):
    return isinstance(value,str) and NAME.fullmatch(value) is not None


def case_file(path, extensions):
    if not isinstance(path,str):
        raise ValueError('Choose a file in the case')
    relative = canonical_path(path)
    if relative.split('/')[0].casefold() == '.vibeflow' or PurePosixPath(relative).suffix.lower() not in extensions:
        raise ValueError('Choose a supported material file in the case')
    return relative


def definitions(materials, folder=PurePosixPath('.')):
    if not isinstance(materials,dict) or len(materials)>64:
        raise ValueError('Use a material library with up to 64 entries')
    parsed = {}
    for name, material in materials.items():
        if (not valid_name(name) or not isinstance(material,dict) or set(material)-{'type','doubles','textures'}
                or not isinstance(material.get('type'),str) or material['type'] not in TYPES):
            raise ValueError('OSPRay material needs a valid name and shader type')
        numbers, textures = material.get('doubles',{}), material.get('textures',{})
        if (not isinstance(numbers,dict) or not isinstance(textures,dict) or len(numbers)+len(textures)>48
                or any(not valid_name(key) for key in (*numbers,*textures))):
            raise ValueError('Invalid OSPRay shader properties')
        for value in numbers.values():
            if (not isinstance(value,list) or not 1 <= len(value) <= 16
                    or any(type(x) not in (float,int) or not math.isfinite(x) for x in value)):
                raise ValueError('OSPRay shader values must be finite numeric arrays')
        texture_paths = {key:case_file((folder/PurePosixPath(case_file(path,TEXTURES))).as_posix(),TEXTURES)
                         for key,path in textures.items()}
        parsed[name] = {'type':material['type'], 'doubles':numbers, 'textures':texture_paths}
    return parsed


def normalize(value):
    if not isinstance(value,dict) or set(value)-{'name','file','materials','remove'} or not valid_name(value.get('name')):
        raise ValueError('OSPRay needs the selected material name')
    if len(json.dumps(value,allow_nan=False).encode()) > 128*1024:
        raise ValueError('OSPRay editing request exceeds 128 KiB')
    remove = value.get('remove',[])
    if not isinstance(remove,list) or len(remove)>64 or any(not valid_name(name) for name in remove):
        raise ValueError('OSPRay removals must identify material names')
    file = case_file(value['file'],{'.json'}) if 'file' in value else None
    return {'name':value['name'],'file':file,'materials':definitions(value.get('materials',{})),'remove':remove}


def resolve(root, options):
    materials = {}
    if options['file']:
        file = checked_path(root,options['file'])
        if not file.is_file() or file.stat().st_size > 256*1024:
            raise ValueError('OSPRay library must be a JSON file under 256 KiB')
        document = json.loads(file.read_bytes())
        if not isinstance(document,dict) or document.get('family') != 'OSPRay' or document.get('version') != '0.0':
            raise ValueError('Expected an OSPRay version 0.0 JSON library')
        textures_root = PurePosixPath() if document.get('vibeflow_texture_base') == 'case' else PurePosixPath(options['file']).parent
        materials = definitions(document.get('materials'),textures_root)
    for name in options['remove']:
        materials.pop(name,None)
    materials.update(options['materials'])
    if len(materials)>64:
        raise ValueError('Use a material library with up to 64 entries')
    for material in materials.values():
        for path in material['textures'].values():
            if not checked_path(root,path).is_file():
                raise ValueError('OSPRay texture must be a file in the case')
    return {'name':options['name'], 'file':options['file'], 'materials':materials}
