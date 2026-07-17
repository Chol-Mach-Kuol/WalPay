"""RFC 6238 TOTP and admin authentication.

Implemented directly (~30 lines) rather than adding a dependency: SHA-1 HMAC,
30-second step, 6 digits — compatible with Google Authenticator / Aegis /
FreeOTP. Verification accepts a ±1 step window for clock drift and compares
in constant time.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta, timezone

from ..extensions import db
from ..models import AdminUser, utcnow
from .security import MAX_LOGIN_FAILURES, LOCKOUT_MINUTES, hash_password  # noqa: F401
from werkzeug.security import check_password_hash


def _aware(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_at(secret_b32: str, timestamp: float, digits: int = 6, step: int = 30) -> str:
    key = base64.b32decode(secret_b32)
    counter = struct.pack(">Q", int(timestamp // step))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return f"{code:0{digits}d}"


def verify_totp(secret_b32: str, candidate: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    candidate = candidate.strip()
    ok = False
    for drift in (-1, 0, 1):  # tolerate one step of clock drift either way
        expected = totp_at(secret_b32, now + drift * 30)
        ok |= hmac.compare_digest(expected, candidate)
    return ok


def provisioning_uri(secret_b32: str, email: str) -> str:
    return f"otpauth://totp/WalPay:{email}?secret={secret_b32}&issuer=WalPay"


def authenticate_admin(email: str, password: str, totp_code: str) -> AdminUser | None:
    """Password AND TOTP must both pass. Same lockout policy as the portal.
    Both factors are always checked so timing does not reveal which failed."""
    user = AdminUser.query.filter_by(email=email.strip().lower()).first()
    if user is None:
        check_password_hash(hash_password("timing-equalizer"), password)
        return None
    if user.locked_until and _aware(user.locked_until) > utcnow():
        return None
    password_ok = check_password_hash(user.password_hash, password)
    totp_ok = verify_totp(user.totp_secret, totp_code)
    if password_ok and totp_ok:
        user.failed_logins = 0
        user.locked_until = None
        db.session.commit()
        return user
    user.failed_logins += 1
    if user.failed_logins >= MAX_LOGIN_FAILURES:
        user.locked_until = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_logins = 0
    db.session.commit()
    return None
