"""Voucher state machine, redemption security, expiry, and payout tests."""
from datetime import timedelta

import pytest

from app.models import AccountType, VoucherStatus, utcnow
from app.services.ledger import account_balance, get_or_create_account
from app.services.vouchers import (
    VoucherError,
    expire_voucher,
    issue_voucher,
    pay_out_voucher,
    redeem_voucher,
)


def _issue(seed, key="pay:1"):
    return issue_voucher(
        sender_id=seed["sender"].id,
        bundle_id=seed["bundle"].id,
        recipient_phone="+211911111111",
        fee_cents=100,
        payment_idempotency_key=key,
    )


def test_full_happy_path(db, seed):
    voucher, code = _issue(seed)
    assert voucher.status == VoucherStatus.ISSUED
    assert len(code) == 8

    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    assert account_balance(cash) == 1600  # 1500 value + 100 fee

    redeemed = redeem_voucher(code, "+211911111111", seed["provider"].id)
    assert redeemed.status == VoucherStatus.REDEEMED
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, f"provider:{seed['provider'].id}")
    assert account_balance(payable) == -1500  # owed to provider

    paid = pay_out_voucher(redeemed, "mgurush:tx123")
    assert paid.status == VoucherStatus.PAID_OUT
    assert account_balance(payable) == 0
    assert account_balance(cash) == 100  # only the fee remains as revenue cash


def test_wrong_phone_fails_and_locks_after_max_attempts(app, db, seed):
    voucher, code = _issue(seed)
    for _ in range(app.config["REDEMPTION_MAX_ATTEMPTS"]):
        with pytest.raises(VoucherError):
            redeem_voucher(code, "+211999999999", seed["provider"].id)
    with pytest.raises(VoucherError) as exc:
        redeem_voucher(code, "+211911111111", seed["provider"].id)  # correct, but locked
    assert exc.value.code == "locked"


def test_wrong_provider_cannot_redeem(db, seed):
    voucher, code = _issue(seed)
    with pytest.raises(VoucherError):
        redeem_voucher(code, "+211911111111", seed["provider"].id + 99)
    assert voucher.failed_attempts == 1


def test_double_redemption_blocked(db, seed):
    voucher, code = _issue(seed)
    redeem_voucher(code, "+211911111111", seed["provider"].id)
    with pytest.raises(VoucherError) as exc:
        redeem_voucher(code, "+211911111111", seed["provider"].id)
    assert exc.value.code == "not_redeemable"


def test_expiry_books_refund_and_blocks_redemption(db, seed):
    voucher, code = _issue(seed)
    voucher.expires_at = utcnow() - timedelta(days=1)
    db.session.commit()

    with pytest.raises(VoucherError) as exc:
        redeem_voucher(code, "+211911111111", seed["provider"].id)
    assert exc.value.code == "expired"

    expire_voucher(voucher)
    assert voucher.status == VoucherStatus.EXPIRED
    refund = get_or_create_account(AccountType.REFUND_PAYABLE, f"sender:{seed['sender'].id}")
    assert account_balance(refund) == -1500  # face value owed back; fee retained

    # Idempotent: expiring again changes nothing.
    expire_voucher(voucher)
    assert account_balance(refund) == -1500


def test_payout_before_redemption_is_illegal(db, seed):
    voucher, _ = _issue(seed)
    with pytest.raises(VoucherError) as exc:
        pay_out_voucher(voucher, "mgurush:early")
    assert exc.value.code == "illegal_transition"


def test_plaintext_code_never_stored(db, seed):
    voucher, code = _issue(seed)
    assert code not in voucher.code_hash
    assert voucher.code_hash != code
