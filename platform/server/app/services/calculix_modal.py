"""Read modes and harmonic displacements from native CalculiX DAT data."""

import math
import re


_STEP = re.compile(r'^[ \t]*S T E P[ \t]+(\d+)[ \t]*\r?$', re.MULTILINE)
_TITLE = 'E I G E N V A L U E   O U T P U T'
_FREQUENCY = re.compile(r'^[ \t]*P A R T I C I P A T I O N[ \t]+F A C T O R S[ \t]+F O R[ \t]+'
                        r'F R E Q U E N C Y[ \t]+(\S+)[ \t]+\(CYCLES/TIME\)', re.MULTILINE)
_DISPLACEMENT = re.compile(r'^[ \t]*displacements \(vx,vy,vz\) for set (.+?) and time[ \t]+(\S+)[ \t]*\r?$',
                           re.MULTILINE | re.IGNORECASE)


def _sections(text, heading):
    matches = list(heading.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index+1].start() if index+1 < len(matches) else len(text)
        yield match, text[match.end():end]


def _number(value):
    number = float(value.replace('D','E').replace('d','e'))
    if not math.isfinite(number):
        raise ValueError('Native CalculiX result is non-finite')
    return number


def read_modes(data):
    result = []
    for step, text in _sections(data.decode('ascii', errors='replace'), _STEP):
        _, title, body = text.partition(_TITLE)
        if not title:
            continue
        modes = []
        for line in body.splitlines():
            fields = line.split()
            if not fields or not fields[0].isdigit():
                if modes:
                    break
                continue
            if len(fields) != 5:
                raise ValueError('Unsupported CalculiX frequency table columns')
            mode = int(fields[0])
            eigenvalue, angular, frequency, imaginary = map(_number, fields[1:])
            if mode < 1 or (modes and mode <= modes[-1]['mode']):
                raise ValueError('Native CalculiX modal numbers are not strictly increasing')
            modes.append({'mode':mode, 'eigenvalue':eigenvalue, 'frequency':frequency,
                          'angular_frequency':angular, 'imaginary_angular_frequency':imaginary})
        if not modes:
            raise ValueError('Native CalculiX frequency table contains no modes')
        result.append({'step':int(step[1]),'modes':modes})
    return result


def _node_displacement(block, node):
    for line in block.lstrip().splitlines():
        values = line.split()
        if not values:
            break
        if len(values) != 4 or not values[0].isdigit():
            raise ValueError('Malformed CalculiX nodal displacement')
        if int(values[0]) == node:
            return list(map(_number, values[1:]))
    return None


def read_harmonics(data, *, node_set, node):
    result = []
    for step, text in _sections(data.decode('ascii', errors='replace'), _STEP):
        samples = []
        frequencies = []
        for header, response in _sections(text, _FREQUENCY):
            frequency = _number(header[1])
            if frequency < 0 or (frequencies and frequency <= frequencies[-1]):
                raise ValueError('CalculiX harmonic frequencies must be nonnegative and strictly increasing')
            frequencies.append(frequency)
            parts = []
            for field, region in _sections(response, _DISPLACEMENT):
                if field[1].casefold() != node_set.casefold():
                    continue
                if not math.isclose(_number(field[2]),frequency,rel_tol=1e-6,abs_tol=1e-12):
                    raise ValueError('CalculiX displacement frequency differs from the native harmonic step')
                parts.append(_node_displacement(region,node))
            if not parts or all(part is None for part in parts):
                continue
            if len(parts) != 2 or any(part is None for part in parts):
                raise ValueError('CalculiX harmonic displacement needs a real and imaginary vector at every frequency')
            real, imaginary = parts
            amplitude = [math.hypot(x,y) for x,y in zip(real,imaginary)]
            if not all(math.isfinite(value) for value in amplitude):
                raise ValueError('CalculiX harmonic displacement amplitude is non-finite')
            samples.append({'frequency':frequency, 'real':real, 'imaginary':imaginary, 'amplitude':amplitude,
                            'phase_radians':[math.atan2(y,x) for x,y in zip(real,imaginary)]})
        if samples:
            if len(samples) != len(frequencies):
                raise ValueError('CalculiX harmonic displacement is missing from a native frequency')
            result.append({'step':int(step[1]),'samples':samples})
    return result
