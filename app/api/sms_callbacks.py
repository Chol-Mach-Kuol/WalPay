"""Africa's Talking delivery report callback.

AT posts form-encoded fields (id, status, failureReason). We authenticate the
callback with a shared token in the URL query string, since AT does not sign
delivery reports: configure the callback URL in the AT dashboard as
  https://<host>/api/sms/delivery-report?token=<AT_CALLBACK_TOKEN>
"""
import hmac
import os

from flask import Blueprint, jsonify, request

from ..services.sms import record_delivery_report

bp = Blueprint("sms_callbacks", __name__)

_DELIVERED = {"Success"}
_FAILED = {"Failed", "Rejected"}


@bp.post("/delivery-report")
def delivery_report():
    expected = os.environ.get("AT_CALLBACK_TOKEN", "")
    provided = request.args.get("token", "")
    if not expected or not hmac.compare_digest(expected, provided):
        return jsonify({"error": "unauthorized"}), 401

    gateway_id = request.form.get("id", "")
    status = request.form.get("status", "")
    if not gateway_id or not status:
        return jsonify({"error": "malformed report"}), 400

    if status in _DELIVERED:
        known = record_delivery_report(gateway_id, delivered=True, failure_reason=None)
    elif status in _FAILED:
        known = record_delivery_report(
            gateway_id, delivered=False, failure_reason=request.form.get("failureReason")
        )
    else:
        return jsonify({"status": "ignored"}), 200  # e.g. 'Sent' interim states

    # Unknown ids get 200 so AT stops retrying; we log nothing sensitive.
    return jsonify({"status": "recorded" if known else "unknown_id"}), 200
