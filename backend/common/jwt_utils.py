"""Minimal HS256 JWT and PBKDF2 password hashing using only the standard library."""
import base64
import hashlib
import hmac
import json
import secrets
import time


class TokenError(Exception):
    pass


def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(signing_input, secret):
    return hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()


def sign(claims, secret, ttl_seconds=8 * 3600, now=None):
    issued = int(now if now is not None else time.time())
    payload = {**claims, "iat": issued, "exp": issued + ttl_seconds}
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = _b64e(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return signing_input + "." + _b64e(_signature(signing_input, secret))


def verify(token, secret, now=None):
    if not token or not isinstance(token, str):
        raise TokenError("missing token")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed token")
    try:
        header = json.loads(_b64d(parts[0]))
        payload = json.loads(_b64d(parts[1]))
        signature = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError):
        raise TokenError("malformed token") from None
    if header.get("alg") != "HS256":
        raise TokenError("unsupported algorithm")
    if not hmac.compare_digest(signature, _signature(parts[0] + "." + parts[1], secret)):
        raise TokenError("bad signature")
    current = now if now is not None else time.time()
    if not isinstance(payload.get("exp"), (int, float)) or payload["exp"] <= current:
        raise TokenError("token expired")
    return payload


def hash_password(password, salt=None, iterations=100_000):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"pbkdf2${iterations}${salt}${digest}"


def verify_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(candidate, digest)
