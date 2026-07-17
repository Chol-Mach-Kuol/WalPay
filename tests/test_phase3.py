"""Phase 3 tests: storefront checkout, CSRF, portal auth + lockout, redemption UI."""
import pytest

from app.models import IntentStatus, ProviderUser, PurchaseIntent, SmsMessage, VoucherStatus
from app.extensions import db as sa_db
from app.web.security import MAX_LOGIN_FAILURES, hash_password


@pytest.fixture()
def portal_user(db, seed):
    user = ProviderUser(
        provider_id=seed["provider"].id,
        email="reception@jubacare.example",
        password_hash=hash_password("correct-horse-battery"),
    )
    db.session.add(user)
    db.session.commit()
    return user


def _csrf(client, path="/voucher/status"):
    """Read the session CSRF token. The token is per-session, not per-page, so
    any page with a form works; the status page always has one."""
    html = client.get(path).get_data(as_text=True)
    return html.split('name="_csrf" value="')[1].split('"')[0]


def _buy(client, seed, phone="+211911111111"):
    token = _csrf(client, f"/checkout/{seed['bundle'].id}")
    resp = client.post(
        f"/checkout/{seed['bundle'].id}",
        data={
            "_csrf": token,
            "full_name": "Diaspora Sender",
            "email": "sender@example.com",
            "recipient_phone": phone,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    public_id = resp.headers["Location"].rsplit("/", 1)[-1]
    return public_id


# --- Storefront ----------------------------------------------------------------
def test_index_lists_verified_provider_and_bundle(client, seed):
    html = client.get("/").get_data(as_text=True)
    assert "Juba Care Clinic" in html
    assert "Malaria test + treatment" in html
    assert "$15.00" in html


def test_checkout_requires_csrf(client, seed):
    resp = client.post(
        f"/checkout/{seed['bundle'].id}",
        data={"full_name": "X Y", "email": "a@b.co", "recipient_phone": "+211911111111"},
    )
    assert resp.status_code == 400


def test_checkout_validates_phone_format(client, seed):
    token = _csrf(client, f"/checkout/{seed['bundle'].id}")
    resp = client.post(
        f"/checkout/{seed['bundle'].id}",
        data={"_csrf": token, "full_name": "X Y", "email": "a@b.co", "recipient_phone": "0912345"},
    )
    assert resp.status_code == 422
    assert PurchaseIntent.query.count() == 0


def test_full_checkout_and_simulated_payment_issues_voucher(client, db, seed):
    public_id = _buy(client, seed)
    intent = PurchaseIntent.query.filter_by(public_id=public_id).one()
    assert intent.status == IntentStatus.PENDING
    assert intent.fee_cents == 90  # 6% of 1500

    token = _csrf(client)
    resp = client.post(f"/purchase/{public_id}/simulate-payment", data={"_csrf": token})
    assert resp.status_code == 302

    sa_db.session.refresh(intent)
    assert intent.status == IntentStatus.COMPLETED
    assert intent.voucher_id is not None
    assert SmsMessage.query.filter_by(purpose="voucher_code").count() == 1

    # Replaying the simulator cannot issue a second voucher (idempotent).
    token = _csrf(client)
    client.post(f"/purchase/{public_id}/simulate-payment", data={"_csrf": token})
    assert SmsMessage.query.filter_by(purpose="voucher_code").count() == 1


def test_simulator_disabled_outside_dev_mode(app, client, seed):
    app.config["PSP_CHECKOUT_MODE"] = "redirect"
    public_id = _buy(client, seed)
    token = _csrf(client)
    resp = client.post(f"/purchase/{public_id}/simulate-payment", data={"_csrf": token})
    assert resp.status_code == 404


def test_voucher_status_lookup(client, db, seed):
    public_id = _buy(client, seed)
    token = _csrf(client)
    client.post(f"/purchase/{public_id}/simulate-payment", data={"_csrf": token})
    code = SmsMessage.query.one().body.split("code: ")[1].split(".")[0]

    token = _csrf(client, "/voucher/status")
    html = client.post(
        "/voucher/status",
        data={"_csrf": token, "code": code, "recipient_phone": "+211911111111"},
    ).get_data(as_text=True)
    assert "READY TO USE" in html

    # Wrong phone reveals nothing.
    token = _csrf(client, "/voucher/status")
    html = client.post(
        "/voucher/status",
        data={"_csrf": token, "code": code, "recipient_phone": "+211999999999"},
    ).get_data(as_text=True)
    assert "READY TO USE" not in html


# --- Portal ---------------------------------------------------------------------
def _login(client, email="reception@jubacare.example", password="correct-horse-battery"):
    token = _csrf(client, "/portal/login")
    return client.post(
        "/portal/login", data={"_csrf": token, "email": email, "password": password}
    )


def test_dashboard_requires_login(client, seed):
    resp = client.get("/portal/")
    assert resp.status_code == 302 and "/portal/login" in resp.headers["Location"]


def test_login_success_and_dashboard_renders(client, portal_user, seed):
    resp = _login(client)
    assert resp.status_code == 302
    html = client.get("/portal/").get_data(as_text=True)
    assert "Juba Care Clinic" in html and "Awaiting payout" in html


def test_login_wrong_password_then_lockout(client, db, portal_user):
    for _ in range(MAX_LOGIN_FAILURES):
        assert _login(client, password="wrong").status_code == 401
    # Correct password is now rejected: account locked.
    assert _login(client).status_code == 401
    db.session.refresh(portal_user)
    assert portal_user.locked_until is not None


def test_portal_redeem_happy_path(client, db, seed, portal_user):
    public_id = _buy(client, seed)
    token = _csrf(client)
    client.post(f"/purchase/{public_id}/simulate-payment", data={"_csrf": token})
    code = SmsMessage.query.one().body.split("code: ")[1].split(".")[0]

    _login(client)
    token = _csrf(client)
    resp = client.post(
        "/portal/redeem",
        data={"_csrf": token, "code": code, "recipient_phone": "+211911111111"},
        follow_redirects=True,
    )
    html = resp.get_data(as_text=True)
    assert "Redeemed: Malaria test + treatment" in html
    assert "$15.00" in html

    intent = PurchaseIntent.query.filter_by(public_id=public_id).one()
    from app.models import Voucher

    assert sa_db.session.get(Voucher, intent.voucher_id).status == VoucherStatus.REDEEMED


def test_portal_redeem_wrong_code_shows_generic_error(client, seed, portal_user):
    _login(client)
    token = _csrf(client)
    resp = client.post(
        "/portal/redeem",
        data={"_csrf": token, "code": "WRONGCOD", "recipient_phone": "+211911111111"},
        follow_redirects=True,
    )
    assert "does not match" in resp.get_data(as_text=True)
