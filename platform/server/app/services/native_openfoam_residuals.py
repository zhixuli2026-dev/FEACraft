"""Read native residual samples and compare them with declared linear tolerances."""

import math
import re


RE_RESIDUAL = re.compile(
    r"(?:smoothSolver|GAMG|GAMGPCG|GAMGPAI|PCG|PBiCGStab|DIC|DILU|diagonal)[^,]*?"
    r"[Ss]olving for\s+([\w.]+)[^,]*,\s*Initial residual\s*=\s*([\d.eE+-]+),"
    r"\s*Final residual\s*=\s*([\d.eE+-]+),\s*No Iterations\s*(\d+)")
_TELEMETRY = re.compile(
    r'(?:residual\s*=|^Time\s*=)\s*([+-]?(?:nan|inf(?:inity)?|'
    r'(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?))(?![\w.])', re.I | re.M)


def has_nonfinite_telemetry(text):
    return any(not math.isfinite(float(match[1])) for match in _TELEMETRY.finditer(text))


def residual_samples(text):
    for match in RE_RESIDUAL.finditer(text):
        try:
            initial,final,iterations=float(match[2]),float(match[3]),int(match[4])
        except ValueError:
            continue
        if math.isfinite(initial) and math.isfinite(final):
            yield match[1],{'initial':initial,'final':final,'iterations':iterations}


def residual_evidence(declarations, detail):
    result={}
    for field,target in declarations.items():
        observations=detail.get(field) or [row for axis in 'xyz' for row in detail.get(field+axis,())]
        failed=sum(row['initial']<0 or row['final']<0 or row['final']>max(target['tolerance'],target['relTol']*row['initial'])
                   for row in observations)
        result[field]={'samples':len(observations),'out_of_tolerance':failed,
            'maximum_final_residual':max((row['final'] for row in observations),default=None),
            'status':'missing' if not observations else 'unconverged' if failed else 'converged'}
    return result
