"""Provider portal: session-authenticated redemption for clinic staff.

Replaces the v1 API-key endpoint for human users (the API endpoint remains
for future POS integrations). Staff log in, type the patient's code and
phone, and see the redemption result immediately.
"""
from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func

from ...extensions import db
from ...models import Provider, Voucher, VoucherStatus
from ...services.vouchers import VoucherError, redeem_voucher
from ..security import (
    authenticate_provider_user,
    login_provider_user,
    provider_login_required,
    require_csrf,
)

bp = Blueprint("portal", __name__, template_folder="../templates", url_prefix="/portal")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        user = authenticate_provider_user(
            request.form.get("email", ""), request.form.get("password", "")
        )
        if user is None:
            # One message for wrong password, unknown email, or locked account.
            flash("Sign-in failed. Check your details or try again in 15 minutes.", "error")
            return render_template("portal/login.html"), 401
        login_provider_user(user)
        return redirect(url_for("portal.dashboard"))
    return render_template("portal/login.html")


@bp.post("/logout")
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("portal.login"))


@bp.get("/")
@provider_login_required
def dashboard():
    provider = db.session.get(Provider, session["provider_id"])
    pending_cents = (
        db.session.query(func.coalesce(func.sum(Voucher.face_value_cents), 0))
        .filter(Voucher.provider_id == provider.id, Voucher.status == VoucherStatus.REDEEMED)
        .scalar()
    )
    recent = (
        Voucher.query.filter(
            Voucher.provider_id == provider.id,
            Voucher.status.in_([VoucherStatus.REDEEMED, VoucherStatus.PAID_OUT]),
        )
        .order_by(Voucher.redeemed_at.desc())
        .limit(10)
        .all()
    )
    return render_template(
        "portal/dashboard.html", provider=provider, pending_cents=int(pending_cents), recent=recent
    )


@bp.post("/redeem")
@provider_login_required
def redeem():
    require_csrf()
    code = request.form.get("code", "").strip()
    phone = request.form.get("recipient_phone", "").strip().replace(" ", "")
    if not code or not phone:
        flash("Enter both the voucher code and the patient's phone number.", "error")
        return redirect(url_for("portal.dashboard"))
    try:
        voucher = redeem_voucher(code=code, recipient_phone=phone, provider_id=session["provider_id"])
    except VoucherError as exc:
        messages = {
            "locked": "This voucher is locked after too many failed attempts. Contact support.",
            "expired": "This voucher has expired. The sender will be refunded automatically.",
            "not_redeemable": "This voucher was already used or is no longer valid.",
        }
        flash(messages.get(exc.code, "Code, phone number, or clinic does not match."), "error")
        return redirect(url_for("portal.dashboard"))
    flash(
        f"Redeemed: {voucher.bundle.title} — "
        f"${voucher.face_value_cents // 100}.{voucher.face_value_cents % 100:02d} "
        f"will be included in your next payout.",
        "ok",
    )
    return redirect(url_for("portal.dashboard"))
