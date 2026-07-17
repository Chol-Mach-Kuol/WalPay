"""Phase 4 tests: TOTP, admin login, dispute money flow, callbacks, reconciliation."""
import time

import pytest

from app.extensions import db as sa_db
from app.models import (
    AccountType,
    AdminUser,
    CallbackCheck,
    CallbackStatus,
    VoucherStatus,
)
from app.services.ledger import account_balance, get_or_create_account
from app.services.reports import reconciliation_report, resolve_callback, sample_callbacks
from app.services.vouchers import (
    VoucherError,
    dispute_voucher,
    issue_voucher,
    redeem_voucher,
    resolve_dispute,
)
from app.web.admin_auth import generate_totp_secret, totp_at, verify_totp
from app.web.security import hash_password


def _redeemed_voucher(seed, key="pay:p4"):
    voucher, code = issue_voucher(
        sender_id=seed["sender"].id,
        bundle_id=seed["bundle"].id,
        recipient_phone="+211911111111",
        fee_cents=100,
        payment_idempotency_key=key,
    )
    return redeem_voucher(code, "+211911111111", seed["provider"].id)


# --- TOTP -------------------------------------------------------------------
def test_totp_verifies_current_and_adjacent_windows():
    secret = generate_totp_secret()
    now = time.time()
    assert verify_totp(secret, totp_at(secret, now), now=now)
    assert verify_totp(secret, totp_at(secret, now - 30), now=now)   # drift back
    assert verify_totp(secret, totp_at(secret, now + 30), now=now)   # drift fwd
    assert not verify_totp(secret, totp_at(secret, now - 120), now=now)
    assert not verify_totp(secret, "000000", now=now) or totp_at(secret, now) == "000000"


def test_totp_rfc6238_vector():
    # RFC 6238 test vector: secret "12345678901234567890" at T=59 -> 94287082
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_at(secret, 59, digits=8) == "94287082"


# --- Admin auth ----------------------------------------------------------------
@pytest.fixture()
def admin(db):
    secret = generate_totp_secret()
    user = AdminUser(
        email="ops@healthvoucher.example",
        password_hash=hash_password("admin-pass"),
        totp_secret=secret,
    )
    db.session.add(user)
    db.session.commit()
    return user, secret


def _admin_csrf(client):
    html = client.get("/admin/login").get_data(as_text=True)
    return html.split('name="_csrf" value="')[1].split('"')[0]


def test_admin_login_requires_valid_totp(client, admin):
    user, secret = admin
    token = _admin_csrf(client)
    resp = client.post("/admin/login", data={
        "_csrf": token, "email": user.email, "password": "admin-pass", "totp": "000000"})
    assert resp.status_code == 401
    token = _admin_csrf(client)
    resp = client.post("/admin/login", data={
        "_csrf": token, "email": user.email, "password": "admin-pass",
        "totp": totp_at(secret, time.time())})
    assert resp.status_code == 302
    assert "Reconciliation" in client.get("/admin/").get_data(as_text=True)


def test_admin_dashboard_requires_login(client, seed):
    resp = client.get("/admin/")
    assert resp.status_code == 302 and "/admin/login" in resp.headers["Location"]


# --- Disputes -------------------------------------------------------------------
def test_dispute_excludes_from_payout_and_refund_claws_back(db, seed):
    voucher = _redeemed_voucher(seed)
    dispute_voucher(voucher)
    assert voucher.status == VoucherStatus.DISPUTED

    from app.services.jobs import create_payout_batch

    batch, _ = create_payout_batch()
    assert batch is None  # disputed voucher is not payable

    resolve_dispute(voucher, "refund")
    assert voucher.status == VoucherStatus.REFUNDED
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, f"provider:{seed['provider'].id}")
    refund = get_or_create_account(AccountType.REFUND_PAYABLE, f"sender:{seed['sender'].id}")
    assert account_balance(payable) == 0
    assert account_balance(refund) == -1500


def test_dispute_resolved_pay_releases_to_provider(db, seed):
    voucher = _redeemed_voucher(seed, key="pay:p4b")
    dispute_voucher(voucher)
    resolve_dispute(voucher, "pay", reference="manual-review-ok")
    assert voucher.status == VoucherStatus.PAID_OUT


def test_dispute_invalid_outcome(db, seed):
    voucher = _redeemed_voucher(seed, key="pay:p4c")
    dispute_voucher(voucher)
    with pytest.raises(VoucherError):
        resolve_dispute(voucher, "shrug")


# --- Callbacks -------------------------------------------------------------------
def test_callback_sampling_and_flag_disputes(db, seed):
    voucher = _redeemed_voucher(seed, key="pay:p4d")
    queued = sample_callbacks(percent=100)
    assert queued == 1
    assert sample_callbacks(percent=100) == 0  # never re-sample the same voucher

    check = CallbackCheck.query.one()
    resolve_callback(check, verified=False, note="patient says never visited")
    assert check.status == CallbackStatus.FLAGGED
    assert voucher.status == VoucherStatus.DISPUTED


def test_callback_verified_leaves_voucher_alone(db, seed):
    voucher = _redeemed_voucher(seed, key="pay:p4e")
    sample_callbacks(percent=100)
    resolve_callback(CallbackCheck.query.one(), verified=True, note="confirmed treated")
    assert voucher.status == VoucherStatus.REDEEMED


# --- Reconciliation ---------------------------------------------------------------
def test_reconciliation_report_balances(db, seed):
    _redeemed_voucher(seed, key="pay:p4f")
    report = reconciliation_report()
    assert report["trial_balance_zero"] is True
    assert report["cash_covers_obligations"] is True
    assert report["cash_cents"] == 1600
    assert report["obligations_cents"] == 1500  # provider payable; fee is revenue


# --- Production boot gate ----------------------------------------------------
def test_production_refuses_dev_secrets(monkeypatch):
    import pytest as _pytest

    from app import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with _pytest.raises(RuntimeError, match="Refusing to start"):
        create_app()
