"""Voucher lifecycle engine.

Every state change is paired with its ledger transaction inside the same DB
transaction, so money state and voucher state can never diverge.

Money flow (all integer cents, USD):
  purchase:  platform_cash +(value+fee) | escrow_liability -(value) | fee_revenue -(fee)
  redeem:    escrow_liability +(value)  | provider_payable -(value)
  payout:    provider_payable +(value)  | platform_cash -(value)
  expire/refund: escrow_liability +(value) | refund_payable -(value)

Sign convention: entries are signed and must sum to zero per transaction;
liability balances are negative while owed. Tests assert the invariants.
"""
from datetime import timedelta, timezone


def _aware(dt):
    """Normalize DB datetimes to UTC-aware. SQLite drops tzinfo on storage;
    PostgreSQL with timestamptz does not. This keeps comparisons safe on both."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

from flask import current_app

from ..extensions import db
from ..models import (
    ALLOWED_TRANSITIONS,
    AccountType,
    ServiceBundle,
    Voucher,
    VoucherStatus,
    utcnow,
)
from . import codes
from .ledger import get_or_create_account, post_transaction


class VoucherError(Exception):
    """Domain error with a machine-readable code for API responses."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _transition(voucher: Voucher, new_status: VoucherStatus) -> None:
    if new_status not in ALLOWED_TRANSITIONS[voucher.status]:
        raise VoucherError(
            "illegal_transition",
            f"Cannot move voucher {voucher.id} from {voucher.status.value} to {new_status.value}.",
        )
    voucher.status = new_status


def issue_voucher(
    sender_id: int,
    bundle_id: int,
    recipient_phone: str,
    fee_cents: int,
    payment_idempotency_key: str,
) -> tuple[Voucher, str]:
    """Create a voucher after a confirmed payment. Returns (voucher, plaintext_code).

    The plaintext code is returned exactly once for SMS delivery and never stored.
    """
    bundle = db.session.get(ServiceBundle, bundle_id)
    if bundle is None or not bundle.is_active:
        raise VoucherError("bundle_unavailable", "Service bundle not found or inactive.")
    if not bundle.provider.is_verified or not bundle.provider.is_active:
        raise VoucherError("provider_unavailable", "Provider is not currently accepting vouchers.")
    if fee_cents < 0:
        raise VoucherError("invalid_fee", "Fee cannot be negative.")

    plaintext = codes.generate_code()
    voucher = Voucher(
        sender_id=sender_id,
        provider_id=bundle.provider_id,
        bundle_id=bundle.id,
        face_value_cents=bundle.price_cents,
        fee_cents=fee_cents,
        code_hash=codes.hash_code(plaintext),
        recipient_phone=recipient_phone,
        expires_at=utcnow() + timedelta(days=current_app.config["VOUCHER_EXPIRY_DAYS"]),
    )
    db.session.add(voucher)
    db.session.flush()

    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, f"voucher:{voucher.id}")
    fees = get_or_create_account(AccountType.FEE_REVENUE)
    entries = [(cash, bundle.price_cents + fee_cents), (escrow, -bundle.price_cents)]
    if fee_cents:
        entries.append((fees, -fee_cents))
    post_transaction(
        f"Voucher {voucher.id} purchased",
        entries,
        idempotency_key=payment_idempotency_key,
        voucher_id=voucher.id,
    )
    db.session.commit()
    return voucher, plaintext


def complete_purchase(
    sender_id: int,
    bundle_id: int,
    recipient_phone: str,
    fee_cents: int,
    idempotency_key: str,
):
    """Issue a voucher AND queue its code SMS as one unit. Used by both the
    PSP webhook and the dev checkout simulator so behavior cannot diverge."""
    from .sms import enqueue_sms  # local import to avoid a cycle

    voucher, code = issue_voucher(
        sender_id=sender_id,
        bundle_id=bundle_id,
        recipient_phone=recipient_phone,
        fee_cents=fee_cents,
        payment_idempotency_key=idempotency_key,
    )
    enqueue_sms(
        to_phone=voucher.recipient_phone,
        body=(
            f"WalPay code: {code}. Show this code and this phone at the "
            f"clinic to receive your prepaid care. Do not share it with anyone else."
        ),
        purpose="voucher_code",
        voucher_id=voucher.id,
    )
    db.session.commit()
    return voucher


def redeem_voucher(code: str, recipient_phone: str, provider_id: int) -> Voucher:
    """Redeem at a specific provider. Moves escrow to provider payable."""
    voucher = Voucher.query.filter_by(code_hash=codes.hash_code(code)).first()

    # Do not reveal whether the code or the phone was wrong.
    generic = VoucherError("invalid_redemption", "Code, phone number, or provider does not match.")
    if voucher is None:
        raise generic

    if voucher.locked_at is not None:
        raise VoucherError("locked", "This voucher is locked after too many failed attempts.")

    if voucher.recipient_phone != recipient_phone or voucher.provider_id != provider_id:
        voucher.failed_attempts += 1
        if voucher.failed_attempts >= current_app.config["REDEMPTION_MAX_ATTEMPTS"]:
            voucher.locked_at = utcnow()
        db.session.commit()
        raise generic

    if voucher.status != VoucherStatus.ISSUED:
        raise VoucherError("not_redeemable", f"Voucher is {voucher.status.value}.")
    if utcnow() >= _aware(voucher.expires_at):
        raise VoucherError("expired", "Voucher has expired; the sender will be refunded.")

    _transition(voucher, VoucherStatus.REDEEMED)
    voucher.redeemed_at = utcnow()

    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, f"voucher:{voucher.id}")
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, f"provider:{provider_id}")
    post_transaction(
        f"Voucher {voucher.id} redeemed at provider {provider_id}",
        [(escrow, voucher.face_value_cents), (payable, -voucher.face_value_cents)],
        idempotency_key=f"redeem:{voucher.id}",
        voucher_id=voucher.id,
    )
    db.session.commit()
    return voucher


def expire_voucher(voucher: Voucher) -> Voucher:
    """Expire an unredeemed voucher and book the sender refund. Idempotent."""
    if voucher.status == VoucherStatus.EXPIRED:
        return voucher
    if utcnow() < _aware(voucher.expires_at):
        raise VoucherError("not_yet_expired", "Voucher has not reached its expiry date.")
    _transition(voucher, VoucherStatus.EXPIRED)

    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, f"voucher:{voucher.id}")
    refund = get_or_create_account(AccountType.REFUND_PAYABLE, f"sender:{voucher.sender_id}")
    post_transaction(
        f"Voucher {voucher.id} expired; refund owed to sender {voucher.sender_id}",
        [(escrow, voucher.face_value_cents), (refund, -voucher.face_value_cents)],
        idempotency_key=f"expire:{voucher.id}",
        voucher_id=voucher.id,
    )
    db.session.commit()
    return voucher


def pay_out_voucher(voucher: Voucher, payout_reference: str) -> Voucher:
    """Mark a redeemed voucher as paid to the provider (manual payout in v1)."""
    _transition(voucher, VoucherStatus.PAID_OUT)
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, f"provider:{voucher.provider_id}")
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    post_transaction(
        f"Voucher {voucher.id} paid out ({payout_reference})",
        [(payable, voucher.face_value_cents), (cash, -voucher.face_value_cents)],
        idempotency_key=f"payout:{voucher.id}",
        voucher_id=voucher.id,
    )
    db.session.commit()
    return voucher


def dispute_voucher(voucher: Voucher) -> Voucher:
    """Pull a redeemed voucher out of payout runs pending investigation.
    No money moves: the value stays parked in provider_payable until resolved."""
    _transition(voucher, VoucherStatus.DISPUTED)
    db.session.commit()
    return voucher


def resolve_dispute(voucher: Voucher, outcome: str, reference: str = "") -> Voucher:
    """Resolve a dispute: 'pay' releases to the provider as normal; 'refund'
    claws the value back from provider_payable into a sender refund."""
    if outcome == "pay":
        return pay_out_voucher(voucher, payout_reference=reference or "dispute:resolved-pay")
    if outcome == "refund":
        _transition(voucher, VoucherStatus.REFUNDED)
        payable = get_or_create_account(
            AccountType.PROVIDER_PAYABLE, f"provider:{voucher.provider_id}"
        )
        refund = get_or_create_account(AccountType.REFUND_PAYABLE, f"sender:{voucher.sender_id}")
        post_transaction(
            f"Voucher {voucher.id} dispute refunded to sender {voucher.sender_id}",
            [(payable, voucher.face_value_cents), (refund, -voucher.face_value_cents)],
            idempotency_key=f"dispute-refund:{voucher.id}",
            voucher_id=voucher.id,
        )
        db.session.commit()
        return voucher
    raise VoucherError("invalid_outcome", "Dispute outcome must be 'pay' or 'refund'.")
