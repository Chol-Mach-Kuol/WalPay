"""Phase 2 tests: outbox reliability, delivery reports, expiry sweep, payouts."""
from datetime import timedelta

from app.models import (
    AccountType,
    SmsMessage,
    SmsStatus,
    Voucher,
    VoucherStatus,
    utcnow,
)
from app.services.jobs import (
    confirm_payout_batch,
    create_payout_batch,
    expire_due_vouchers,
)
from app.services.ledger import account_balance, get_or_create_account
from app.services.sms import (
    MAX_ATTEMPTS,
    ConsoleGateway,
    SmsGatewayError,
    enqueue_sms,
    process_outbox,
    record_delivery_report,
)
from app.services.vouchers import issue_voucher, redeem_voucher


class FlakyGateway:
    """Fails N times, then behaves like ConsoleGateway."""

    def __init__(self, failures: int):
        self.failures = failures
        self.inner = ConsoleGateway()

    def send(self, to_phone, body):
        if self.failures > 0:
            self.failures -= 1
            raise SmsGatewayError("simulated outage")
        return self.inner.send(to_phone, body)


def _issue(seed, key="pay:sms1"):
    return issue_voucher(
        sender_id=seed["sender"].id,
        bundle_id=seed["bundle"].id,
        recipient_phone="+211911111111",
        fee_cents=100,
        payment_idempotency_key=key,
    )


# --- Outbox ----------------------------------------------------------------
def test_outbox_sends_and_records_gateway_id(app, db):
    enqueue_sms("+211911111111", "hello", "test")
    db.session.commit()
    gw = ConsoleGateway()
    stats = process_outbox(gateway=gw)
    assert stats["sent"] == 1
    msg = SmsMessage.query.one()
    assert msg.status == SmsStatus.SENT
    assert msg.gateway_message_id == "console-1"
    assert gw.sent == [("+211911111111", "hello")]


def test_outbox_retries_with_backoff_then_succeeds(app, db):
    enqueue_sms("+211911111111", "retry me", "test")
    db.session.commit()
    gw = FlakyGateway(failures=1)

    stats = process_outbox(gateway=gw)
    msg = SmsMessage.query.one()
    assert stats["retried"] == 1 and msg.status == SmsStatus.QUEUED
    assert msg.attempts == 1 and msg.last_error == "simulated outage"

    # Not due yet: worker must skip it rather than hammer the gateway.
    assert process_outbox(gateway=gw)["skipped"] == 1

    # Force due, then it sends.
    msg.next_attempt_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    assert process_outbox(gateway=gw)["sent"] == 1
    assert SmsMessage.query.one().status == SmsStatus.SENT


def test_outbox_marks_failed_after_max_attempts(app, db):
    enqueue_sms("+211911111111", "doomed", "test")
    db.session.commit()
    gw = FlakyGateway(failures=99)
    msg = SmsMessage.query.one()
    for _ in range(MAX_ATTEMPTS):
        msg.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
        process_outbox(gateway=gw)
    assert SmsMessage.query.one().status == SmsStatus.FAILED


def test_gateway_outage_never_loses_messages(app, db):
    """The core resilience promise: total outage leaves everything queued."""
    for i in range(3):
        enqueue_sms(f"+21191111111{i}", f"msg {i}", "test")
    db.session.commit()
    process_outbox(gateway=FlakyGateway(failures=99))
    assert SmsMessage.query.filter_by(status=SmsStatus.QUEUED).count() == 3


# --- Delivery reports --------------------------------------------------------
def test_delivery_report_marks_delivered(app, db):
    enqueue_sms("+211911111111", "hi", "test")
    db.session.commit()
    process_outbox(gateway=ConsoleGateway())
    assert record_delivery_report("console-1", delivered=True, failure_reason=None)
    assert SmsMessage.query.one().status == SmsStatus.DELIVERED


def test_delivery_report_failure_requeues(app, db):
    enqueue_sms("+211911111111", "hi", "test")
    db.session.commit()
    process_outbox(gateway=ConsoleGateway())
    record_delivery_report("console-1", delivered=False, failure_reason="AbsentSubscriber")
    msg = SmsMessage.query.one()
    assert msg.status == SmsStatus.QUEUED and msg.last_error == "AbsentSubscriber"


def test_delivery_report_unknown_id(app, db):
    assert record_delivery_report("nope", delivered=True, failure_reason=None) is False


def test_delivery_callback_requires_token(client, monkeypatch):
    monkeypatch.setenv("AT_CALLBACK_TOKEN", "sekret")
    resp = client.post("/api/sms/delivery-report?token=wrong", data={"id": "x", "status": "Success"})
    assert resp.status_code == 401


# --- Webhook integration ------------------------------------------------------
def test_payment_webhook_queues_code_sms(client, db, seed):
    payload = {
        "event_id": "evt_sms",
        "event": "charge.completed",
        "source": "flutterwave",
        "data": {
            "sender_id": seed["sender"].id,
            "bundle_id": seed["bundle"].id,
            "recipient_phone": "+211911111111",
            "fee_cents": 100,
        },
    }
    client.post("/api/webhooks/payments", json=payload, headers={"verif-hash": "test-webhook-secret"})
    msg = SmsMessage.query.filter_by(purpose="voucher_code").one()
    assert msg.to_phone == "+211911111111"
    # The queued body must contain a working code: redeem with it.
    code = msg.body.split("code: ")[1].split(".")[0]
    voucher = redeem_voucher(code, "+211911111111", seed["provider"].id)
    assert voucher.status == VoucherStatus.REDEEMED


# --- Expiry job ---------------------------------------------------------------
def test_expiry_job_expires_refunds_and_notifies(app, db, seed):
    voucher, _ = _issue(seed, key="pay:exp1")
    voucher.expires_at = utcnow() - timedelta(days=1)
    db.session.commit()

    assert expire_due_vouchers() == 1
    assert voucher.status == VoucherStatus.EXPIRED
    assert SmsMessage.query.filter_by(purpose="voucher_expired").count() == 1
    # Re-run is a no-op.
    assert expire_due_vouchers() == 0


# --- Payout batches -------------------------------------------------------------
def test_payout_batch_export_and_confirm(app, db, seed):
    v1, c1 = _issue(seed, key="pay:po1")
    v2, c2 = _issue(seed, key="pay:po2")
    redeem_voucher(c1, "+211911111111", seed["provider"].id)
    redeem_voucher(c2, "+211911111111", seed["provider"].id)

    batch, csv_text = create_payout_batch()
    assert batch is not None
    assert f"{batch.id},{seed['provider'].id},3000,2" in csv_text

    # A second export before confirmation must not re-batch the same vouchers.
    batch2, _ = create_payout_batch()
    assert batch2 is None

    paid = confirm_payout_batch(batch.id, "mgurush:tx999")
    assert paid == 2
    assert v1.status == VoucherStatus.PAID_OUT and v2.status == VoucherStatus.PAID_OUT
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, f"provider:{seed['provider'].id}")
    assert account_balance(payable) == 0

    # Confirming again cannot double-pay (idempotent per voucher).
    assert confirm_payout_batch(batch.id, "mgurush:tx999") == 0
