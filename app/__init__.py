"""WalPay application factory.

Phase 1 scope: ledger, voucher engine, webhook ingestion, redemption API.
No medical data is stored anywhere in this system by design.
"""
import logging
import os

from flask import Flask, g, request
import uuid

from .extensions import db


def create_app(config_object: str | None = None) -> Flask:
    app = Flask(__name__)

    # --- Configuration -----------------------------------------------------
    # All secrets come from environment variables. Nothing is hardcoded.
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///walpay_dev.db"
    )
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-not-for-prod")
    app.config["VOUCHER_CODE_PEPPER"] = os.environ.get(
        "VOUCHER_CODE_PEPPER", "dev-pepper-not-for-prod"
    )
    app.config["VOUCHER_EXPIRY_DAYS"] = int(os.environ.get("VOUCHER_EXPIRY_DAYS", "90"))
    app.config["REDEMPTION_MAX_ATTEMPTS"] = int(
        os.environ.get("REDEMPTION_MAX_ATTEMPTS", "5")
    )
    if config_object:
        app.config.from_object(config_object)

    # Production hard gate: refuse to boot with development fallbacks.
    if os.environ.get("APP_ENV", "development") == "production":
        insecure = []
        if app.config["SECRET_KEY"] == "dev-only-not-for-prod":
            insecure.append("SECRET_KEY")
        if app.config["VOUCHER_CODE_PEPPER"] == "dev-pepper-not-for-prod":
            insecure.append("VOUCHER_CODE_PEPPER")
        if not os.environ.get("PSP_WEBHOOK_SECRET"):
            insecure.append("PSP_WEBHOOK_SECRET")
        if insecure:
            raise RuntimeError(
                f"Refusing to start in production with unset secrets: {', '.join(insecure)}"
            )

    db.init_app(app)
    from flask_migrate import Migrate

    Migrate(app, db)

    # --- Structured logging with correlation IDs ---------------------------
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)r}',
    )

    @app.before_request
    def attach_correlation_id():
        g.correlation_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

    @app.after_request
    def echo_correlation_id(response):
        response.headers["X-Request-ID"] = g.get("correlation_id", "")
        # Baseline security headers for all responses.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # --- Blueprints ---------------------------------------------------------
    from .api.webhooks import bp as webhooks_bp
    from .api.redemption import bp as redemption_bp
    from .api.sms_callbacks import bp as sms_callbacks_bp
    from .cli import bp as ops_bp
    from .web.shop import bp as shop_bp
    from .web.portal import bp as portal_bp
    from .web.admin import bp as admin_bp

    app.register_blueprint(webhooks_bp, url_prefix="/api/webhooks")
    app.register_blueprint(redemption_bp, url_prefix="/api/redemption")
    app.register_blueprint(sms_callbacks_bp, url_prefix="/api/sms")
    app.register_blueprint(ops_bp)
    app.register_blueprint(shop_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(admin_bp)

    # Session cookie hardening. SECURE is on unless explicitly disabled for
    # local HTTP development.
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault(
        "SESSION_COOKIE_SECURE", os.environ.get("COOKIE_SECURE", "1") == "1"
    )
    app.config.setdefault("PSP_CHECKOUT_MODE", os.environ.get("PSP_CHECKOUT_MODE", "dev"))
    app.config.setdefault("FEE_PERCENT", int(os.environ.get("FEE_PERCENT", "6")))

    @app.template_filter("usd")
    def usd_filter(cents: int) -> str:
        """Integer-only dollar formatting: floats never touch money, even here."""
        sign = "-" if cents < 0 else ""
        cents = abs(int(cents))
        return f"{sign}{cents // 100}.{cents % 100:02d}"

    from .web.security import csrf_token

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": csrf_token}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app
