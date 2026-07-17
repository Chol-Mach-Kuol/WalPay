"""Provider-facing redemption endpoint.

v1 auth: per-provider API key (hash stored server-side; issued during manual
onboarding). Phase 3 replaces this with the provider portal session. Rate
limiting note: put a reverse-proxy limit (e.g. 10 req/min/IP) in front of
this route in deployment; the per-voucher attempt lockout in the service
layer is the second line of defense.
"""
import hashlib
import hmac
import os

from flask import Blueprint, jsonify, request

from ..services.vouchers import VoucherError, redeem_voucher

bp = Blueprint("redemption", __name__)


def _provider_id_from_key(api_key: str) -> int | None:
    """v1 keyring: PROVIDER_API_KEYS env var as 'provider_id:sha256hex,...'."""
    keyring = os.environ.get("PROVIDER_API_KEYS", "")
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    for pair in filter(None, keyring.split(",")):
        pid, _, stored = pair.partition(":")
        if stored and hmac.compare_digest(stored, digest):
            return int(pid)
    return None


@bp.post("/redeem")
def redeem():
    api_key = request.headers.get("X-Provider-Key", "")
    provider_id = _provider_id_from_key(api_key) if api_key else None
    if provider_id is None:
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    code = str(body.get("code", "")).strip()
    phone = str(body.get("recipient_phone", "")).strip()
    if not code or not phone:
        return jsonify({"error": "code and recipient_phone are required"}), 400

    try:
        voucher = redeem_voucher(code=code, recipient_phone=phone, provider_id=provider_id)
    except VoucherError as exc:
        status = 423 if exc.code == "locked" else 422
        return jsonify({"error": exc.code, "message": str(exc)}), status

    return (
        jsonify(
            {
                "status": "redeemed",
                "voucher_id": voucher.id,
                "bundle_id": voucher.bundle_id,
                "face_value_cents": voucher.face_value_cents,
            }
        ),
        200,
    )
