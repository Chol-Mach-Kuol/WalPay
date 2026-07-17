"""SMS delivery via a transactional outbox.

Reliability design:
- enqueue_sms() only writes a row — it participates in the caller's DB
  transaction, so a voucher and its code SMS are committed atomically.
- process_outbox() is run by a worker/cron. It sends due messages through a
  pluggable gateway and applies exponential backoff on failure:
  1m, 5m, 25m, ~2h, ~10h — then marks FAILED for human follow-up.
- The gateway is injected so tests (and dev) never touch the network, and an
  Africa's Talking outage degrades to "codes arrive late", never "codes lost".

Never log full message bodies: they contain redemption codes.
"""
from datetime import timedelta, timezone
import os

from flask import current_app

from ..extensions import db
from ..models import SmsMessage, SmsStatus, utcnow

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60  # 60 * 5^n


def _aware(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class SmsGatewayError(Exception):
    """Transient gateway failure — the message stays queued for retry."""


class AfricasTalkingGateway:
    """Thin client for Africa's Talking bulk SMS."""

    URL = "https://api.africastalking.com/version1/messaging"

    def send(self, to_phone: str, body: str) -> str:
        import requests

        api_key = os.environ.get("AT_API_KEY")
        username = os.environ.get("AT_USERNAME")
        if not api_key or not username:
            raise SmsGatewayError("Africa's Talking credentials not configured.")
        try:
            resp = requests.post(
                self.URL,
                headers={"apiKey": api_key, "Accept": "application/json"},
                data={"username": username, "to": to_phone, "message": body},
                timeout=15,
            )
        except requests.RequestException as exc:  # network problem: retryable
            raise SmsGatewayError(f"network: {exc.__class__.__name__}") from exc
        if resp.status_code >= 500:
            raise SmsGatewayError(f"gateway 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise SmsGatewayError(f"gateway rejected: {resp.status_code}")
        recipients = (resp.json().get("SMSMessageData", {}) or {}).get("Recipients", [])
        if not recipients or recipients[0].get("statusCode") not in (100, 101, 102):
            raise SmsGatewayError("gateway did not accept recipient")
        return recipients[0].get("messageId", "")


class ConsoleGateway:
    """Dev/test gateway: prints instead of sending."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self._counter = 0

    def send(self, to_phone: str, body: str) -> str:
        self._counter += 1
        self.sent.append((to_phone, body))
        return f"console-{self._counter}"


def get_gateway():
    if os.environ.get("SMS_GATEWAY", "console") == "africastalking":
        return AfricasTalkingGateway()
    return ConsoleGateway()


def enqueue_sms(to_phone: str, body: str, purpose: str, voucher_id: int | None = None) -> SmsMessage:
    msg = SmsMessage(to_phone=to_phone, body=body, purpose=purpose, voucher_id=voucher_id)
    db.session.add(msg)
    db.session.flush()
    return msg


def process_outbox(gateway=None, limit: int = 50) -> dict:
    """Send all due queued messages. Returns counters for logging/monitoring."""
    gateway = gateway or get_gateway()
    now = utcnow()
    due = (
        SmsMessage.query.filter(SmsMessage.status == SmsStatus.QUEUED)
        .order_by(SmsMessage.next_attempt_at)
        .limit(limit)
        .all()
    )
    stats = {"sent": 0, "retried": 0, "failed": 0, "skipped": 0}
    for msg in due:
        if _aware(msg.next_attempt_at) > now:
            stats["skipped"] += 1
            continue
        try:
            gateway_id = gateway.send(msg.to_phone, msg.body)
        except SmsGatewayError as exc:
            msg.attempts += 1
            msg.last_error = str(exc)[:255]
            if msg.attempts >= MAX_ATTEMPTS:
                msg.status = SmsStatus.FAILED
                stats["failed"] += 1
                current_app.logger.error(
                    f"SMS {msg.id} ({msg.purpose}) permanently failed after {msg.attempts} attempts."
                )
            else:
                delay = BACKOFF_BASE_SECONDS * (5 ** (msg.attempts - 1))
                msg.next_attempt_at = now + timedelta(seconds=delay)
                stats["retried"] += 1
        else:
            msg.status = SmsStatus.SENT
            msg.gateway_message_id = gateway_id or None
            stats["sent"] += 1
    db.session.commit()
    return stats


def record_delivery_report(gateway_message_id: str, delivered: bool, failure_reason: str | None) -> bool:
    """Apply an Africa's Talking delivery report. Returns False if unknown id."""
    msg = SmsMessage.query.filter_by(gateway_message_id=gateway_message_id).first()
    if msg is None:
        return False
    if delivered:
        msg.status = SmsStatus.DELIVERED
    else:
        # Carrier-level failure after gateway acceptance: requeue once with a
        # short delay; if it has already been retried post-send, mark failed.
        if msg.attempts < MAX_ATTEMPTS:
            msg.status = SmsStatus.QUEUED
            msg.attempts += 1
            msg.next_attempt_at = utcnow() + timedelta(minutes=5)
            msg.last_error = (failure_reason or "carrier failure")[:255]
        else:
            msg.status = SmsStatus.FAILED
    db.session.commit()
    return True
