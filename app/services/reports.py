"""Reconciliation reporting and the anti-fraud callback queue."""
import secrets

from sqlalchemy import func

from ..extensions import db
from ..models import (
    AccountType,
    CallbackCheck,
    CallbackStatus,
    LedgerAccount,
    LedgerEntry,
    Voucher,
    VoucherStatus,
    utcnow,
)
from .vouchers import dispute_voucher

CALLBACK_SAMPLE_PERCENT = 20  # call roughly 1 in 5 redemptions


def balances_by_type() -> dict[str, int]:
    rows = (
        db.session.query(LedgerAccount.type, func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
        .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
        .group_by(LedgerAccount.type)
        .all()
    )
    totals = {t.value: 0 for t in AccountType}
    for type_, total in rows:
        totals[type_.value] = int(total)
    return totals


def reconciliation_report() -> dict:
    """Trial balance plus the operational check that matters: cash on hand must
    exactly cover everything owed outward (escrow + payables)."""
    totals = balances_by_type()
    trial_balance = sum(totals.values())
    obligations = -(
        totals["escrow_liability"] + totals["provider_payable"] + totals["refund_payable"]
    )
    return {
        "totals": totals,
        "trial_balance_zero": trial_balance == 0,
        "cash_cents": totals["platform_cash"],
        "obligations_cents": obligations,
        "cash_covers_obligations": totals["platform_cash"] >= obligations,
        "voucher_counts": dict(
            db.session.query(Voucher.status, func.count())
            .group_by(Voucher.status)
            .all()
        ),
    }


def sample_callbacks(percent: int = CALLBACK_SAMPLE_PERCENT) -> int:
    """Queue a random sample of un-checked redemptions for patient callbacks.
    Uses cryptographic randomness so a colluding provider cannot predict
    which redemptions will be verified."""
    already = db.session.query(CallbackCheck.voucher_id)
    candidates = (
        Voucher.query.filter(
            Voucher.status.in_([VoucherStatus.REDEEMED, VoucherStatus.PAID_OUT])
        )
        .filter(~Voucher.id.in_(already))
        .all()
    )
    queued = 0
    for voucher in candidates:
        if secrets.randbelow(100) < percent:
            db.session.add(CallbackCheck(voucher_id=voucher.id))
            queued += 1
    db.session.commit()
    return queued


def resolve_callback(check: CallbackCheck, verified: bool, note: str = "") -> CallbackCheck:
    """Record the call outcome. A flagged check auto-disputes the voucher if it
    has not been paid out yet; paid-out flags still surface for recovery."""
    check.status = CallbackStatus.VERIFIED if verified else CallbackStatus.FLAGGED
    check.note = note[:255] or None
    check.resolved_at = utcnow()
    if not verified and check.voucher.status == VoucherStatus.REDEEMED:
        dispute_voucher(check.voucher)
    db.session.commit()
    return check
