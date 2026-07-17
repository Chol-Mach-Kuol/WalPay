"""Operational jobs (run via Flask CLI / cron).

- expire_due_vouchers: sweeps ISSUED vouchers past expiry, books refunds,
  and notifies the recipient by SMS that the voucher lapsed.
- create_payout_batch: gathers all REDEEMED vouchers into a batch and returns
  CSV rows for the operator to pay via m-Gurush/bank.
- confirm_payout_batch: after paying, the operator confirms with a payment
  reference; every voucher in the batch transitions to PAID_OUT atomically
  with its ledger posting.
"""
import csv
import io
from collections import defaultdict

from ..extensions import db
from ..models import PayoutBatch, PayoutItem, Voucher, VoucherStatus, utcnow
from .sms import enqueue_sms
from .vouchers import expire_voucher, pay_out_voucher


def expire_due_vouchers(batch_size: int = 200) -> int:
    """Expire all due vouchers. Safe to run repeatedly (idempotent per voucher)."""
    due = (
        Voucher.query.filter(
            Voucher.status == VoucherStatus.ISSUED, Voucher.expires_at <= utcnow()
        )
        .limit(batch_size)
        .all()
    )
    for voucher in due:
        expire_voucher(voucher)
        enqueue_sms(
            to_phone=voucher.recipient_phone,
            body=(
                "WalPay: your voucher has expired and the sender will be "
                "refunded. Ask them to purchase a new one if care is still needed."
            ),
            purpose="voucher_expired",
            voucher_id=voucher.id,
        )
    db.session.commit()
    return len(due)


def create_payout_batch() -> tuple[PayoutBatch | None, str]:
    """Snapshot every REDEEMED voucher into a new batch; return (batch, csv)."""
    already_batched = db.session.query(PayoutItem.voucher_id)
    redeemed = (
        Voucher.query.filter(Voucher.status == VoucherStatus.REDEEMED)
        .filter(~Voucher.id.in_(already_batched))
        .all()
    )
    if not redeemed:
        return None, ""

    batch = PayoutBatch()
    db.session.add(batch)
    db.session.flush()

    totals: dict[int, int] = defaultdict(int)
    for voucher in redeemed:
        db.session.add(PayoutItem(batch_id=batch.id, voucher_id=voucher.id))
        totals[voucher.provider_id] += voucher.face_value_cents
    db.session.commit()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["batch_id", "provider_id", "total_usd_cents", "voucher_count"])
    counts: dict[int, int] = defaultdict(int)
    for v in redeemed:
        counts[v.provider_id] += 1
    for provider_id, cents in sorted(totals.items()):
        writer.writerow([batch.id, provider_id, cents, counts[provider_id]])
    return batch, buf.getvalue()


def confirm_payout_batch(batch_id: int, reference: str) -> int:
    """Mark every voucher in the batch paid out. Idempotent per voucher via the
    ledger's payout idempotency key; a re-run cannot double-pay."""
    batch = db.session.get(PayoutBatch, batch_id)
    if batch is None:
        raise ValueError(f"No payout batch {batch_id}.")
    items = PayoutItem.query.filter_by(batch_id=batch_id).all()
    paid = 0
    for item in items:
        voucher = db.session.get(Voucher, item.voucher_id)
        if voucher.status == VoucherStatus.REDEEMED:
            pay_out_voucher(voucher, payout_reference=reference)
            paid += 1
    batch.reference = reference
    batch.confirmed_at = utcnow()
    db.session.commit()
    return paid
