"""认证与加密工具：PBKDF2 密码哈希（stdlib）+ JWT"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt

from .config import get_settings

_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


def create_token(user_id: int) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(hours=s.jwt_ttl_hours)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> int | None:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=["HS256"])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
