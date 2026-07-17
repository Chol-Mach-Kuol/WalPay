"""Admin console: reconciliation, disputes, and the patient-callback queue.

Access requires password + TOTP. All state changes are CSRF-protected POSTs.
"""
import functools

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ...extensions import db
from ...models import CallbackCheck, CallbackStatus, Voucher, VoucherStatus
from ...services.reports import reconciliation_report, resolve_callback, sample_callbacks
from ...services.vouchers import VoucherError, resolve_dispute
from ..admin_auth import authenticate_admin
from ..security import csrf_token, require_csrf

bp = Blueprint("admin", __name__, template_folder="../templates", url_prefix="/admin")


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "admin_user_id" not in session:
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        require_csrf()
        user = authenticate_admin(
            request.form.get("email", ""),
            request.form.get("password", ""),
            request.form.get("totp", ""),
        )
        if user is None:
            flash("Sign-in failed. Check your email, password, and authenticator code.", "error")
            return render_template("admin/login.html"), 401
        session.clear()
        session["admin_user_id"] = user.id
        csrf_token()
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/login.html")


@bp.post("/logout")
def logout():
    require_csrf()
    session.clear()
    return redirect(url_for("admin.login"))


@bp.get("/")
@admin_required
def dashboard():
    report = reconciliation_report()
    disputes = Voucher.query.filter_by(status=VoucherStatus.DISPUTED).all()
    callbacks = (
        CallbackCheck.query.filter_by(status=CallbackStatus.PENDING)
        .order_by(CallbackCheck.created_at)
        .limit(20)
        .all()
    )
    return render_template(
        "admin/dashboard.html", report=report, disputes=disputes, callbacks=callbacks
    )


@bp.post("/callbacks/sample")
@admin_required
def callbacks_sample():
    require_csrf()
    queued = sample_callbacks()
    flash(f"Queued {queued} redemption(s) for patient callbacks.", "ok")
    return redirect(url_for("admin.dashboard"))


@bp.post("/callbacks/<int:check_id>/resolve")
@admin_required
def callbacks_resolve(check_id: int):
    require_csrf()
    check = db.session.get(CallbackCheck, check_id)
    if check is None or check.status != CallbackStatus.PENDING:
        flash("That callback was already resolved.", "error")
        return redirect(url_for("admin.dashboard"))
    verified = request.form.get("outcome") == "verified"
    resolve_callback(check, verified=verified, note=request.form.get("note", ""))
    flash(
        "Marked verified." if verified else "Flagged — the voucher is now disputed and "
        "excluded from payouts.",
        "ok",
    )
    return redirect(url_for("admin.dashboard"))


@bp.post("/disputes/<int:voucher_id>/resolve")
@admin_required
def disputes_resolve(voucher_id: int):
    require_csrf()
    voucher = db.session.get(Voucher, voucher_id)
    if voucher is None:
        flash("Unknown voucher.", "error")
        return redirect(url_for("admin.dashboard"))
    try:
        resolve_dispute(voucher, request.form.get("outcome", ""),
                        reference=request.form.get("reference", ""))
    except VoucherError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))
    flash(f"Dispute on voucher {voucher.id} resolved: {voucher.status.value}.", "ok")
    return redirect(url_for("admin.dashboard"))
