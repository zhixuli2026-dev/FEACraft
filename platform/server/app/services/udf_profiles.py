"""Identify the declarations and artifacts of a supported UDF project."""

import json
from typing import NamedTuple

from . import attempt_workspace


MANIFEST = 'vibeflow-udf-profile.json'
LEGACY = 'functionObject'
BOUNDARY = 'boundaryCondition'
SOURCE = 'sourceTerm'
TURBULENCE = 'turbulenceModel'
SOLVER = 'solver'


class Profile(NamedTuple):
    source: str
    artifact: str
    solver: str


PROFILES = {
    LEGACY: Profile('myFunctionObject.C','libmyFunctionObject.so','icoFoam'),
    BOUNDARY: Profile('VibeflowVelocityBoundary.C','libvibeflowVelocityBoundary.so','icoFoam'),
    SOURCE: Profile('VibeflowAcceleration.C','libvibeflowUniformAcceleration.so','pimpleFoam'),
    TURBULENCE: Profile('VibeflowKEpsilon.C','libvibeflowKEpsilon.so','pimpleFoam'),
    SOLVER: Profile('vibeflowDiffusionFoam.C','vibeflowDiffusionFoam','vibeflowDiffusionFoam'),
}


def declaration(kind):
    if kind not in PROFILES:
        raise ValueError('Unsupported OpenFOAM UDF project type')
    return {'schema':'vibeflow.udf-profile.v1','kind':kind}


def read(root, directory):
    try:
        path = attempt_workspace.checked_path(root,directory+'/'+MANIFEST)
    except FileNotFoundError:
        return LEGACY
    if path.stat().st_size>4096:
        raise ValueError('OpenFOAM UDF profile is too large')
    profile = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(profile,dict) or not isinstance(profile.get('kind'),str) or profile!=declaration(profile['kind']):
        raise ValueError('Invalid OpenFOAM UDF profile')
    return profile['kind']


def artifact(configuration):
    kind = configuration.get('udf_kind',LEGACY)
    return PROFILES[kind].artifact
