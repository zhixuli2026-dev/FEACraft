"""Recognize the original files of an imported single-region OpenFOAM mesh."""

import gzip
import hashlib


DIRECTORY='system/polyMesh'
CORE_FILES=('points','faces','owner','neighbour','boundary')


def core_paths(run):
    original=run/DIRECTORY
    selected={}
    for name in CORE_FILES:
        candidates=[path for path in (original/name,original/(name+'.gz')) if path.is_file()]
        if len(candidates)!=1:
            raise ValueError(f'{DIRECTORY}/{name} requires exactly one plain or gzip mesh file')
        selected[name]=candidates[0]
    return selected


def expanded_record(source):
    digest=hashlib.sha256()
    size=0
    with gzip.open(source,'rb') as stream:
        while chunk:=stream.read(1024*1024):
            size+=len(chunk)
            if size>1024**3:
                raise ValueError('Expanded mesh exceeds the 1 GiB input limit')
            digest.update(chunk)
    return {'size':size,'sha256':digest.hexdigest()}
