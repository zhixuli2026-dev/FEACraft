"""Routes to configured visualization owners in a shared database deployment."""

from ..config import get_settings
from .session_owner_routing import configured_origin


def endpoint(row):
    return configured_origin(row, get_settings().viz_owner_routes)
