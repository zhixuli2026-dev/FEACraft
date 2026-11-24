"""Map the declared Boussinesq reference temperature to native TRef."""

import gzip
import math
import re

from .casedict import parse_casedict


TRANSPORT=('constant/transportProperties','constant/transportProperties.gz')
SOLVERS={'buoyantBoussinesqSimpleFoam','buoyantBoussinesqPimpleFoam'}
DICTIONARY_LIMIT=65536


def declared(case):
    physical=case.get('physicalProblem') or {}
    if not isinstance(physical,dict) or physical.get('referenceTemperatureRole')!='buoyancy':
        return None
    meta=case.get('caseMeta') or {}
    units=(meta.get('units') or {}) if isinstance(meta,dict) else None
    if not isinstance(units,dict):
        raise ValueError('caseMeta.units 需要有效的单位配置')
    unit=units.get('temperature','K')
    if unit not in ('K','degC'):
        raise ValueError('浮力参考温度需要 K 或 degC')
    try:
        if isinstance(physical['referenceTemperature'],bool):
            raise ValueError('Temperature cannot be boolean')
        kelvin=float(physical['referenceTemperature'])+(273.15 if unit=='degC' else 0)
    except (KeyError,TypeError,ValueError,OverflowError) as error:
        raise ValueError('浮力参考温度需要有限温度值') from error
    if not math.isfinite(kelvin) or kelvin<=0:
        raise ValueError('浮力参考温度换算后须大于 0 K')
    return kelvin


def _native_temperature(value):
    if isinstance(value,list):
        entry=re.fullmatch(r'(?:TRef\s+)?\[\s*0\s+0\s+0\s+1\s+0\s+0\s+0\s*\]\s+(.+)', ' '.join(value))
        if not entry:
            raise ValueError('原生 TRef 需要绝对温度量纲')
        value=entry[1]
    try:
        return float(value)
    except (TypeError,ValueError,OverflowError) as error:
        raise ValueError('原生 TRef 需要明确的数值') from error


def prepare(root,run_dir,solver,kelvin,*,require_native=False):
    if kelvin is None:
        return {}
    if solver not in SOLVERS:
        raise ValueError('浮力参考温度需要托管 Boussinesq OpenFOAM 求解器')
    inputs=[run_dir+'/'+name for name in TRANSPORT if (root/run_dir/name).is_file()]
    if len(inputs)!=1:
        raise ValueError('浮力参考温度需要唯一的原生 transportProperties 输入')
    path=inputs[0]
    compressed=path.endswith('.gz')
    open_input=gzip.open if compressed else open
    with open_input(root/path,'rb') as source:
        seed=source.read(DICTIONARY_LIMIT+1)
    if len(seed)>DICTIONARY_LIMIT:
        raise ValueError('浮力 transportProperties 超过原生字典大小限制')
    text=seed.decode('utf-8')
    native=parse_casedict(text)
    if 'TRef' in native:
        current=_native_temperature(native['TRef'])
        if not math.isclose(current,kelvin,rel_tol=0,abs_tol=1e-10):
            raise ValueError('原生 transportProperties.TRef 与声明的浮力参考温度不一致')
        return {}
    if require_native:
        raise ValueError('封存的 transportProperties 缺少 TRef')
    if any(str(key).startswith('#') for key in native):
        raise ValueError('自动补齐 TRef 需要显式的 transportProperties 字典')
    body=seed+f'\nTRef {kelvin!r};\n'.encode('ascii')
    if len(body)>DICTIONARY_LIMIT:
        raise ValueError('补齐 TRef 后 transportProperties 超过原生字典大小限制')
    return {path:gzip.compress(body,mtime=0) if compressed else body}
