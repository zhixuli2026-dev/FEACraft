"""Project mesh quality defaults shared by generated and imported meshes."""


QUALITY_PRESETS={
    'smoke':{'maxNonOrtho':80,'maxSkewness':4,'maxAspectRatio':100},
    'coarse':{'maxNonOrtho':75,'maxSkewness':4,'maxAspectRatio':80},
    'production':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':60},
    'meshIndependence':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':60},
}


def selected(mesh):
    name=mesh.get('qualityPreset')
    return name if isinstance(name,str) and name in QUALITY_PRESETS else None


def effective_targets(mesh,*,include_preset=True):
    if not isinstance(mesh,dict):
        return {}
    explicit=mesh.get('qualityTargets') or {}
    name=selected(mesh) if include_preset else None
    return {'policy':'fail',**QUALITY_PRESETS[name],**explicit} if name else explicit
