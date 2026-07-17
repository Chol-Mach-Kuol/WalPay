"""Payment webhook ingestion.

Every event is persisted once (unique on source + external id), then processed.
Replays return 200 without side effects — PSPs retry aggressively and must
never double-issue a voucher. Signature verification uses the PSP's shared
secret header (Flutterwave-style 'verif-hash'); requests failing it get 401.
"""
import hmac
import json
import os

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import WebhookEvent, utcnow
from ..services.vouchers import VoucherError, complete_purchase

bp = Blueprint("webhooks", __name__)


def _signature_valid(req) -> bool:
    expected = os.environ.get("PSP_WEBHOOK_SECRET", "")
    provided = req.headers.get("verif-hash", "")
    if not expected:  # Fail closed if the secret is not configured.
        current_app.logger.error("PSP_WEBHOOK_SECRET is not set; rejecting webhook.")
        return False
    return hmac.compare_digest(expected, provided)


@bp.post("/payments")
def payment_webhook():
    if not _signature_valid(request):
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True)
    if not payload or "event_id" not in payload:
        return jsonify({"error": "malformed payload"}), 400

    event = WebhookEvent(
        source=payload.get("source", "psp"),
        external_event_id=str(payload["event_id"]),
        payload=json.dumps(payload),
    )
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # Duplicate delivery: acknowledge, do nothing.
        return jsonify({"status": "duplicate_ignored"}), 200

    if payload.get("event") != "charge.completed":
        event.processed_at = utcnow()
        db.session.commit()
        return jsonify({"status": "ignored_event_type"}), 200

    data = payload.get("data", {})
    try:
        voucher = complete_purchase(
            sender_id=int(data["sender_id"]),
            bundle_id=int(data["bundle_id"]),
            recipient_phone=str(data["recipient_phone"]),
            fee_cents=int(data["fee_cents"]),
            idempotency_key=f"payment:{payload['event_id']}",
        )
    except (KeyError, ValueError):
        return jsonify({"error": "invalid charge data"}), 400
    except VoucherError as exc:
        # Money arrived for an unavailable bundle — needs human review + refund.
        current_app.logger.error(f"Voucher issue failed for event {payload['event_id']}: {exc}")
        return jsonify({"error": exc.code}), 409

    event.processed_at = utcnow()
    db.session.commit()

    current_app.logger.info(f"Voucher {voucher.id} issued via webhook; code SMS queued.")
    return jsonify({"status": "voucher_issued", "voucher_id": voucher.id}), 201
