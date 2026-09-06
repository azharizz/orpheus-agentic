"""Small signed owner cookie for the current single-user hosted deployment."""

import base64
import hashlib
import hmac
import secrets
import time
from http.cookies import SimpleCookie

from .. import config

COOKIE = "orpheus_owner"
MAX_AGE = 60 * 60 * 24 * 90


def _signature(owner, issued):
    if not config.OWNER_SECRET:
        raise RuntimeError("ORPHEUS_OWNER_SECRET is required in hosted mode")
    value = f"{owner}.{issued}".encode()
    return hmac.new(config.OWNER_SECRET.encode(), value, hashlib.sha256).hexdigest()


def _pack(owner, issued):
    return f"{owner}.{issued}.{_signature(owner, issued)}"


def _valid(value):
    try:
        owner, issued, signature = value.split(".", 2)
        if len(owner) != 32 or not all(c in "0123456789abcdef" for c in owner):
            return None
        issued = int(issued)
        if not 0 < time.time() - issued <= MAX_AGE:
            return None
        if not hmac.compare_digest(signature, _signature(owner, issued)):
            return None
        return owner
    except (TypeError, ValueError, RuntimeError):
        return None


def resolve(handler):
    """Resolve or mint the owner identity and defer the cookie header to the response."""
    if config.RUNTIME_MODE == "local":
        handler.owner_id = "local"
        handler.owner_cookie = None
        return "local"
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    current = _valid(cookie[COOKIE].value) if COOKIE in cookie else None
    if current:
        owner = current
        handler.owner_cookie = None
    else:
        owner = secrets.token_hex(16)
        handler.owner_cookie = _pack(owner, int(time.time()))
    handler.owner_id = owner
    return owner


def cookie_header(value):
    return f"{COOKIE}={value}; Max-Age={MAX_AGE}; Path=/; HttpOnly; SameSite=Lax; Secure"
