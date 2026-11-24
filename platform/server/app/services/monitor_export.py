"""Bounded operational logs with stable per-process cursors."""
from collections import deque
from itertools import count
import re
import time
from uuid import uuid4


BOOT_ID = uuid4().hex
LOG_CAPACITY = 1024
_EVENTS = deque(maxlen=LOG_CAPACITY)
_SEQUENCE = count(1)
_ROUTE_TEMPLATE = re.compile(r'/[A-Za-z0-9_/.{}:-]{1,200}')
_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}


def record(request, status, started):
    if not request.url.path.startswith('/api/') or request.url.path == '/api/v1/runtime/telemetry':
        return
    template = getattr(request.scope.get('route'), 'path', '')
    route = template if _ROUTE_TEMPLATE.fullmatch(template or '') else '/api/{unmatched}'
    _EVENTS.append({'seq': next(_SEQUENCE), 'time': time.time(), 'route': route,
                    'method': request.method if request.method in _METHODS else 'OTHER',
                    'status': status, 'latency_ms': round((time.perf_counter() - started) * 1000, 2)})


def read(boot, after):
    cursor = after if boot == BOOT_ID else 0
    log = [row for row in _EVENTS if row['seq'] > cursor]
    gap = max(0, log[0]['seq'] - cursor - 1) if log else 0
    return {'boot': BOOT_ID, 'after': log[-1]['seq'] if log else cursor, 'lost': gap, 'records': log}
