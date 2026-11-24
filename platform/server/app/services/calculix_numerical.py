"""Compile declared controls for one native CalculiX static step."""

import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


SOLVERS = {'spooles':'SPOOLES', 'iterativeScaling':'ITERATIVE SCALING', 'iterativeCholesky':'ITERATIVE CHOLESKY'}
STATIC = re.compile(rb'^[ \t]*\*STATIC(?=[ \t,\r\n]|$)[^\r\n]*',re.I | re.M)
OPTIONS = {'linearSolver','staticIncrements'}
INCREMENTS = {'mode','initial','period'}
ADAPTIVE = {'minimum','maximum'}


def declared(value):
    if not isinstance(value,dict) or not value or value.keys()-OPTIONS:
        raise ValueError('CalculiX controls require linearSolver or staticIncrements')
    result = {}
    if 'linearSolver' in value:
        solver = value['linearSolver']
        if not isinstance(solver,str) or solver not in SOLVERS:
            raise ValueError('CalculiX linearSolver requires spooles, iterativeScaling or iterativeCholesky')
        result['linear_solver'] = solver
    if 'staticIncrements' in value:
        increments = value['staticIncrements']
        mode = increments.get('mode') if isinstance(increments,dict) else None
        if mode not in ('fixed','adaptive') or set(increments)!=(INCREMENTS | (ADAPTIVE if mode=='adaptive' else set())):
            raise ValueError('CalculiX staticIncrements requires mode, initial and period, with minimum and maximum for adaptive mode')
        controls = {key:scalar(increments[key]) for key in sorted(set(increments)-{'mode'})}
        initial,period = controls['initial'],controls['period']
        if not 0 < initial <= period:
            raise ValueError('CalculiX requires 0 < initial increment <= period')
        if mode=='adaptive' and not 0 < controls['minimum'] <= initial <= controls['maximum'] <= period:
            raise ValueError('CalculiX adaptive increments require 0 < minimum <= initial <= maximum <= period')
        if mode=='fixed':
            steps = period/initial
            if not math.isfinite(steps) or steps > 2**31-1 or not math.isclose(steps,round(steps),rel_tol=0,abs_tol=1e-8):
                raise ValueError('CalculiX fixed period must consist of a whole number of increments')
        result['increments'] = {'mode':mode,**controls}
    return result


def prepare(root, source, cards):
    try:
        raw = checked_path(root, CASE_DICT_PATH).read_bytes()
        method = parse_casedict(raw.decode('utf-8')).get('numericalMethod')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    if not isinstance(method,dict) or 'calculix' not in method:
        return
    settings = declared(method['calculix'])
    steps = [card.upper().split(',')[1:] for card in cards if card.split(',')[0].upper()=='*STEP']
    if len(steps)!=1 or sum(card.split(',')[0].upper()=='*STATIC' for card in cards)!=1:
        raise ValueError('Managed CalculiX numerical controls require one native static step')
    if 'increments' in settings and not {'NLGEOM','NLGEOM=YES'}.intersection(steps[0]):
        raise ValueError('Managed CalculiX static increments require *STEP,NLGEOM')
    source['numerical'] = settings
    source['files'][CASE_DICT_PATH] = record(raw)


def _native_increment_line(tail):
    for line in tail.splitlines():
        content = line.strip()
        if not content or content.startswith(b'**'):
            continue
        if content.startswith(b'*'):
            return None
        return [scalar(part.replace(b'D',b'E').replace(b'd',b'e').decode('ascii')) for part in content.split(b',')]
    return None


def render(original, settings):
    matches = list(STATIC.finditer(original))
    if len(matches)!=1:
        raise ValueError('Managed CalculiX numerical controls require a top-level *STATIC card')
    match, = matches
    fields = match[0].decode('ascii').upper().split(',')[1:]
    flags = [' '.join(flag.split()) for flag in fields]
    existing_solvers = [param.split('=',1)[1].strip() for param in flags if param.split('=',1)[0].strip()=='SOLVER' and '=' in param]
    additions = []
    solver = settings.get('linear_solver')
    if solver is not None:
        if existing_solvers and existing_solvers!=[SOLVERS[solver]]:
            raise ValueError('Native *STATIC SOLVER conflicts with numericalMethod.calculix')
        if not existing_solvers:
            additions.append('SOLVER='+SOLVERS[solver])
    increments = settings.get('increments')
    data = None
    if increments is not None:
        direct = 'DIRECT' in flags
        native = _native_increment_line(original[match.end():])
        values = [increments['initial'],increments['period']]
        if increments['mode']=='adaptive':
            if direct:
                raise ValueError('Native *STATIC DIRECT conflicts with adaptive CalculiX increments')
            values.extend((increments['minimum'],increments['maximum']))
        elif not direct:
            if native is not None:
                raise ValueError('Native automatic *STATIC increments conflict with declared fixed increments')
            additions.append('DIRECT')
        if native is not None and native!=values:
            raise ValueError('Native *STATIC increments conflict with numericalMethod.calculix')
        if native is None:
            data = ','.join(format(value,'.16g') for value in values).encode('ascii')
    header = match[0].rstrip()+b''.join(b','+part.encode('ascii') for part in additions) if additions else match[0]
    if data is not None:
        header += (b'\r\n' if original[match.end():].startswith(b'\r\n') else b'\n')+data
    return original[:match.start()]+header+original[match.end():]


def observed(root, source, log):
    settings = source['numerical']
    text = log.casefold()
    solver = settings.get('linear_solver')
    result = {}
    if solver=='spooles':
        if 'spooles solver' not in text:
            raise ValueError('Native CalculiX did not report SPOOLES factorization')
    elif solver is not None:
        if ('using the iterative solver' not in text or 'error condition (0=good, 1=bad) = 1' in text
                or 'error condition (0=good, 1=bad) = 0' not in text
                or ('cholesky preconditioning' in text)!=(solver=='iterativeCholesky')):
            raise ValueError('Native CalculiX iterative solver or convergence disagrees with its declaration')
    if solver is not None:
        result['linear_solver'] = solver
    increments = settings.get('increments')
    if increments is not None:
        status = checked_path(root,source['input'][:-4]+'.sta').read_text('ascii')
        rows = re.findall(r'^\s*1[ \t]+(\d+)[ \t]+\d+[ \t]+(\d+)[ \t]+\S+[ \t]+(\S+)[ \t]+(\S+)[ \t]*$',status,re.M)
        times,steps = [],[]
        for index,(increment,iterations,end,step) in enumerate(rows,1):
            size = scalar(step)
            if int(increment)!=index or int(iterations)<1 or size<=0:
                raise ValueError('CalculiX returned inconsistent converged static increments')
            times.append(scalar(end))
            steps.append(size)
        if (not times or 'newton-raphson iterative procedure is active' not in text
                or not math.isclose(times[-1],increments['period'],rel_tol=2e-6)):
            raise ValueError('CalculiX Newton-Raphson did not converge at the configured period')
        if increments['mode']=='adaptive':
            if max(steps)>increments['maximum']*(1+2e-6) or min(steps[:-1] or steps)<increments['minimum']*(1-2e-6):
                raise ValueError('CalculiX used an increment outside the declared adaptive range')
        elif any(not math.isclose(size,increments['initial'],rel_tol=2e-6) for size in steps):
            raise ValueError('CalculiX used an unexpected fixed increment size')
        result.update(algorithm='Newton-Raphson',converged_increments=len(steps),period=times[-1],
                      minimum_increment=min(steps),maximum_increment=max(steps),
                      maximum_iterations=max(int(row[1]) for row in rows))
    return result
