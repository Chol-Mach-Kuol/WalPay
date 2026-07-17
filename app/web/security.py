"""Web security helpers for Phase 3.

- CSRF: a per-session random token, required on every state-changing form
  POST, compared in constant time. Jinja autoescaping covers XSS; session
  cookies are HttpOnly + SameSite=Lax (set in the app factory).
- Passwords: werkzeug's scrypt-based hashing (no plaintext ever stored).
- Login throttling: 5 failures lock the account for 15 minutes.
"""
import functools
import hmac
import secrets
from datetime import timedelta, timezone

from flask import abort, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import ProviderUser, utcnow

MAX_LOGIN_FAILURES = 5
LOCKOUT_MINUTES = 15


def _aware(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --- CSRF -------------------------------------------------------------------
def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def require_csrf() -> None:
    token = session.get("_csrf", "")
    submitted = request.form.get("_csrf", "")
    if not token or not hmac.compare_digest(token, submitted):
        abort(400, description="Invalid or missing CSRF token.")


# --- Passwords ----------------------------------------------------------------
def hash_password(plaintext: str) -> str:
    return generate_password_hash(plaintext, method="scrypt")


# --- Provider login -------------------------------------------------------------
def authenticate_provider_user(email: str, password: str) -> ProviderUser | None:
    """Returns the user on success; None on any failure. Applies lockout.
    Runs a dummy hash check on unknown emails to keep timing uniform."""
    user = ProviderUser.query.filter_by(email=email.strip().lower()).first()
    if user is None:
        check_password_hash(hash_password("timing-equalizer"), password)
        return None
    if user.locked_until and _aware(user.locked_until) > utcnow():
        return None
    if check_password_hash(user.password_hash, password):
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


def login_provider_user(user: ProviderUser) -> None:
    session.clear()  # rotate session on privilege change (fixation defense)
    session["provider_user_id"] = user.id
    session["provider_id"] = user.provider_id
    csrf_token()  # ensure a token exists post-rotation


def provider_login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "provider_user_id" not in session:
            return redirect(url_for("portal.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
