"""Find the configured API origin for a live session owned by another replica."""

import socket
from datetime import timezone

from .durable_events import now


def configured_origin(row, routes):
    hostname = row.owner_id.split(':', 1)[0]
    if (hostname == socket.gethostname() or row.phase not in ('starting', 'active')
            or row.lease_expires_at.replace(tzinfo=timezone.utc) <= now()):
        return None
    origin = routes.get(hostname)
    return origin.rstrip('/') if origin else None
