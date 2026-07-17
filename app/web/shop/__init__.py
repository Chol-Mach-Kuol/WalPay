"""Sender-facing storefront (mobile-first, low-bandwidth).

Flow: browse verified providers -> checkout a bundle -> PSP payment
(dev mode: an explicit simulator button) -> ticket page confirms the code
was SMSed to the patient. A separate lookup page shows voucher status to
whoever holds the code + phone pair.
"""
import math
import re
import uuid

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from ...extensions import db
from ...models import (
    IntentStatus,
    Provider,
    PurchaseIntent,
    Sender,
    ServiceBundle,
    Voucher,
)
from ...services import codes
from ...services.vouchers import VoucherError, complete_purchase

bp = Blueprint("shop", __name__, template_folder="../templates")

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fee_for(amount_cents: int) -> int:
    percent = current_app.config.get("FEE_PERCENT", 6)
    return math.ceil(amount_cents * percent / 100)


@bp.get("/")
def index():
    providers = (
        Provider.query.filter_by(is_verified=True, is_active=True)
        .order_by(Provider.city, Provider.name)
        .all()
    )
    bundles = {
        p.id: [b for b in p.bundles if b.is_active] for p in providers
    }
    return render_template("shop/index.html", providers=providers, bundles=bundles)


@bp.route("/checkout/<int:bundle_id>", methods=["GET", "POST"])
def checkout(bundle_id: int):
    from ..security import require_csrf

    bundle = db.session.get(ServiceBundle, bundle_id)
    if bundle is None or not bundle.is_active or not bundle.provider.is_verified:
        abort(404)
    fee = _fee_for(bundle.price_cents)

    if request.method == "POST":
        require_csrf()
        email = request.form.get("email", "").strip().lower()
        full_name = request.form.get("full_name", "").strip()
        phone = request.form.get("recipient_phone", "").strip().replace(" ", "")
        errors = []
        if not _EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if not (2 <= len(full_name) <= 120):
            errors.append("Enter your full name.")
        if not _PHONE_RE.match(phone):
            errors.append("Enter the patient's phone in international format, e.g. +211912345678.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("shop/checkout.html", bundle=bundle, fee=fee, form=request.form), 422

        sender = Sender.query.filter_by(email=email).first()
        if sender is None:
            sender = Sender(email=email, full_name=full_name, country_code="XX")
            db.session.add(sender)
            db.session.flush()

        intent = PurchaseIntent(
            public_id=str(uuid.uuid4()),
            sender_id=sender.id,
            bundle_id=bundle.id,
            recipient_phone=phone,
            amount_cents=bundle.price_cents,
            fee_cents=fee,
        )
        db.session.add(intent)
        db.session.commit()
        # Prod: redirect to the PSP-hosted payment page here.
        return redirect(url_for("shop.purchase", public_id=intent.public_id))

    return render_template("shop/checkout.html", bundle=bundle, fee=fee, form={})


@bp.get("/purchase/<public_id>")
def purchase(public_id: str):
    intent = PurchaseIntent.query.filter_by(public_id=public_id).first_or_404()
    dev_mode = current_app.config.get("PSP_CHECKOUT_MODE", "dev") == "dev"
    return render_template("shop/purchase.html", intent=intent, dev_mode=dev_mode,
                           IntentStatus=IntentStatus)


@bp.post("/purchase/<public_id>/simulate-payment")
def simulate_payment(public_id: str):
    """Dev-only stand-in for the PSP webhook. Disabled outside dev mode."""
    from ..security import require_csrf

    if current_app.config.get("PSP_CHECKOUT_MODE", "dev") != "dev":
        abort(404)
    require_csrf()
    intent = PurchaseIntent.query.filter_by(public_id=public_id).first_or_404()
    if intent.status == IntentStatus.PENDING:
        try:
            voucher = complete_purchase(
                sender_id=intent.sender_id,
                bundle_id=intent.bundle_id,
                recipient_phone=intent.recipient_phone,
                fee_cents=intent.fee_cents,
                idempotency_key=f"intent:{intent.public_id}",
            )
        except VoucherError as exc:
            flash(str(exc), "error")
            return redirect(url_for("shop.purchase", public_id=public_id))
        intent.status = IntentStatus.COMPLETED
        intent.voucher_id = voucher.id
        db.session.commit()
        flash("Payment received. The voucher code was sent to the patient by SMS.", "ok")
    return redirect(url_for("shop.purchase", public_id=public_id))


@bp.route("/voucher/status", methods=["GET", "POST"])
def voucher_status():
    """Status lookup for whoever holds the code + patient phone. Read-only:
    never reveals the provider's payout state, only what the family needs."""
    from ..security import require_csrf

    result = None
    if request.method == "POST":
        require_csrf()
        code = request.form.get("code", "").strip()
        phone = request.form.get("recipient_phone", "").strip().replace(" ", "")
        if code and phone:
            voucher = Voucher.query.filter_by(code_hash=codes.hash_code(code)).first()
            if voucher and voucher.recipient_phone == phone:
                result = voucher
            else:
                flash("No voucher matches that code and phone number.", "error")
        else:
            flash("Enter both the code and the patient's phone number.", "error")
    return render_template("shop/status.html", result=result)
