"""WalPay data models.

Design principles (see README for full rationale):
- All money is integer USD cents. Floats are banned from the money path.
- The ledger is append-only double-entry: rows are never updated or deleted.
- Voucher status transitions are enforced by a state machine in
  services/vouchers.py AND by a CHECK-style guard here for defense in depth.
- No medical data: bundles are commercial descriptions, never diagnoses.
"""
from datetime import datetime, timezone
import enum

from sqlalchemy import CheckConstraint, UniqueConstraint

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Parties
# --------------------------------------------------------------------------
class Sender(db.Model):
    """A diaspora purchaser. KYC fields captured above regulatory thresholds."""

    __tablename__ = "senders"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    country_code = db.Column(db.String(2), nullable=False)  # ISO 3166-1 alpha-2
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class Provider(db.Model):
    """A verified clinic or pharmacy. Verification is a manual ops process."""

    __tablename__ = "providers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False, unique=True)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    bundles = db.relationship("ServiceBundle", back_populates="provider")


class ServiceBundle(db.Model):
    """A purchasable service, e.g. 'Malaria test + treatment'. Never medical data."""

    __tablename__ = "service_bundles"

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("providers.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    price_cents = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    provider = db.relationship("Provider", back_populates="bundles")

    __table_args__ = (CheckConstraint("price_cents > 0", name="ck_bundle_price_positive"),)


# --------------------------------------------------------------------------
# Vouchers (single-use, v1)
# --------------------------------------------------------------------------
class VoucherStatus(str, enum.Enum):
    ISSUED = "issued"
    REDEEMED = "redeemed"
    PAID_OUT = "paid_out"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


# The single source of truth for legal transitions.
ALLOWED_TRANSITIONS: dict[VoucherStatus, set[VoucherStatus]] = {
    VoucherStatus.ISSUED: {VoucherStatus.REDEEMED, VoucherStatus.EXPIRED, VoucherStatus.REFUNDED},
    VoucherStatus.REDEEMED: {VoucherStatus.PAID_OUT, VoucherStatus.DISPUTED},
    VoucherStatus.DISPUTED: {VoucherStatus.PAID_OUT, VoucherStatus.REFUNDED},
    VoucherStatus.PAID_OUT: set(),   # terminal
    VoucherStatus.EXPIRED: set(),    # terminal (refund transaction happens at expiry)
    VoucherStatus.REFUNDED: set(),   # terminal
}


class Voucher(db.Model):
    __tablename__ = "vouchers"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("senders.id"), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey("providers.id"), nullable=False)
    bundle_id = db.Column(db.Integer, db.ForeignKey("service_bundles.id"), nullable=False)

    # Snapshot pricing at purchase time so later bundle edits can't change value.
    face_value_cents = db.Column(db.Integer, nullable=False)
    fee_cents = db.Column(db.Integer, nullable=False)

    # Redemption security: the plaintext code is shown/SMSed once and only a
    # peppered HMAC is stored. Redemption also requires the recipient phone.
    code_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    recipient_phone = db.Column(db.String(20), nullable=False, index=True)
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    status = db.Column(
        db.Enum(VoucherStatus, values_callable=lambda e: [m.value for m in e]),
        default=VoucherStatus.ISSUED,
        nullable=False,
        index=True,
    )
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    redeemed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    bundle = db.relationship("ServiceBundle")
    provider = db.relationship("Provider")

    __table_args__ = (
        CheckConstraint("face_value_cents > 0", name="ck_voucher_value_positive"),
        CheckConstraint("fee_cents >= 0", name="ck_voucher_fee_nonnegative"),
        CheckConstraint("failed_attempts >= 0", name="ck_voucher_attempts_nonnegative"),
    )


# --------------------------------------------------------------------------
# Double-entry ledger (append-only)
# --------------------------------------------------------------------------
class AccountType(str, enum.Enum):
    PLATFORM_CASH = "platform_cash"          # asset: money held at the PSP
    ESCROW_LIABILITY = "escrow_liability"    # liability: owed to voucher holders
    PROVIDER_PAYABLE = "provider_payable"    # liability: owed to a provider
    FEE_REVENUE = "fee_revenue"              # revenue
    REFUND_PAYABLE = "refund_payable"        # liability: owed back to a sender


class LedgerAccount(db.Model):
    __tablename__ = "ledger_accounts"

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(
        db.Enum(AccountType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Optional owner reference, e.g. provider_payable:42 or escrow_liability:voucher:7
    reference = db.Column(db.String(64), nullable=True)

    __table_args__ = (UniqueConstraint("type", "reference", name="uq_account_type_ref"),)


class LedgerTransaction(db.Model):
    """A balanced group of entries. Entries must sum to zero (service-enforced,
    test-verified). idempotency_key prevents double-posting from webhook replays."""

    __tablename__ = "ledger_transactions"

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(255), nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=True, unique=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    entries = db.relationship("LedgerEntry", back_populates="transaction")


class LedgerEntry(db.Model):
    """One leg of a transaction. Positive = debit-side increase per our sign
    convention: assets positive when they grow, liabilities negative when they
    grow. Simpler rule used here: signed amounts that must sum to zero per txn."""

    __tablename__ = "ledger_entries"

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(
        db.Integer, db.ForeignKey("ledger_transactions.id"), nullable=False, index=True
    )
    account_id = db.Column(
        db.Integer, db.ForeignKey("ledger_accounts.id"), nullable=False, index=True
    )
    amount_cents = db.Column(db.Integer, nullable=False)  # signed; never zero

    transaction = db.relationship("LedgerTransaction", back_populates="entries")
    account = db.relationship("LedgerAccount")

    __table_args__ = (CheckConstraint("amount_cents != 0", name="ck_entry_nonzero"),)


# --------------------------------------------------------------------------
# SMS outbox (Phase 2)
# --------------------------------------------------------------------------
class SmsStatus(str, enum.Enum):
    QUEUED = "queued"
    SENT = "sent"          # accepted by the gateway, awaiting delivery report
    DELIVERED = "delivered"
    FAILED = "failed"      # exhausted retries or terminal gateway rejection


class SmsMessage(db.Model):
    """Transactional outbox: rows are written in the same DB transaction as the
    business event, then a worker delivers them. If Africa's Talking is down,
    messages wait here — the platform never loses a redemption code."""

    __tablename__ = "sms_messages"

    id = db.Column(db.Integer, primary_key=True)
    to_phone = db.Column(db.String(20), nullable=False, index=True)
    body = db.Column(db.String(480), nullable=False)  # up to 3 GSM segments
    purpose = db.Column(db.String(32), nullable=False)  # e.g. voucher_code
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=True)

    status = db.Column(
        db.Enum(SmsStatus, values_callable=lambda e: [m.value for m in e]),
        default=SmsStatus.QUEUED,
        nullable=False,
        index=True,
    )
    attempts = db.Column(db.Integer, default=0, nullable=False)
    next_attempt_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    gateway_message_id = db.Column(db.String(128), nullable=True, unique=True, index=True)
    last_error = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (CheckConstraint("attempts >= 0", name="ck_sms_attempts_nonnegative"),)


class PayoutBatch(db.Model):
    """A weekly manual payout run. Vouchers move to PAID_OUT when the batch is
    confirmed with the operator's payment reference (e.g. m-Gurush txn id)."""

    __tablename__ = "payout_batches"

    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(128), nullable=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class PayoutItem(db.Model):
    __tablename__ = "payout_items"

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("payout_batches.id"), nullable=False, index=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=False, unique=True)

class WebhookEvent(db.Model):
    """Every inbound PSP event is stored exactly once. Replays are ignored."""

    __tablename__ = "webhook_events"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(32), nullable=False)  # e.g. "flutterwave"
    external_event_id = db.Column(db.String(128), nullable=False)
    payload = db.Column(db.Text, nullable=False)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source", "external_event_id", name="uq_webhook_source_event"),
    )


# --------------------------------------------------------------------------
# Web (Phase 3): portal accounts and checkout intents
# --------------------------------------------------------------------------
class ProviderUser(db.Model):
    """A portal login for clinic/pharmacy staff. Replaces v1 API keys."""

    __tablename__ = "provider_users"

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("providers.id"), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    provider = db.relationship("Provider")


class IntentStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class PurchaseIntent(db.Model):
    """A checkout in progress: created before the PSP redirect, completed by
    the payment webhook (prod) or the simulator (dev). Amounts are snapshotted
    so a price change mid-checkout cannot alter what the sender pays."""

    __tablename__ = "purchase_intents"

    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("senders.id"), nullable=False)
    bundle_id = db.Column(db.Integer, db.ForeignKey("service_bundles.id"), nullable=False)
    recipient_phone = db.Column(db.String(20), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    fee_cents = db.Column(db.Integer, nullable=False)
    status = db.Column(
        db.Enum(IntentStatus, values_callable=lambda e: [m.value for m in e]),
        default=IntentStatus.PENDING,
        nullable=False,
    )
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    sender = db.relationship("Sender")
    bundle = db.relationship("ServiceBundle")


# --------------------------------------------------------------------------
# Admin (Phase 4)
# --------------------------------------------------------------------------
class AdminUser(db.Model):
    """Platform operator. Login requires password AND a TOTP code."""

    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    totp_secret = db.Column(db.String(64), nullable=False)  # base32
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)


class CallbackStatus(str, enum.Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FLAGGED = "flagged"


class CallbackCheck(db.Model):
    """A randomly sampled redemption queued for a human phone call to the
    patient — the anti-collusion control software can't provide by itself."""

    __tablename__ = "callback_checks"

    id = db.Column(db.Integer, primary_key=True)
    voucher_id = db.Column(db.Integer, db.ForeignKey("vouchers.id"), nullable=False, unique=True)
    status = db.Column(
        db.Enum(CallbackStatus, values_callable=lambda e: [m.value for m in e]),
        default=CallbackStatus.PENDING,
        nullable=False,
        index=True,
    )
    note = db.Column(db.String(255), nullable=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    voucher = db.relationship("Voucher")
