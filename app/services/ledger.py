"""Double-entry ledger service.

Invariants enforced here (and verified by tests):
1. Every transaction's entries sum to exactly zero.
2. A transaction has at least two entries.
3. Entries are append-only: this module exposes no update/delete.
4. An idempotency key can post at most once; replays return the original txn.
5. Escrow and payable accounts may never go negative.
"""
from sqlalchemy import func

from ..extensions import db
from ..models import AccountType, LedgerAccount, LedgerEntry, LedgerTransaction


class LedgerError(Exception):
    """Raised when a posting would violate a ledger invariant."""


def get_or_create_account(type_: AccountType, reference: str | None = None) -> LedgerAccount:
    account = LedgerAccount.query.filter_by(type=type_, reference=reference).first()
    if account is None:
        account = LedgerAccount(type=type_, reference=reference)
        db.session.add(account)
        db.session.flush()
    return account


def account_balance(account: LedgerAccount) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
        .filter(LedgerEntry.account_id == account.id)
        .scalar()
    )
    return int(total)


# Accounts that represent money owed outward and must never dip below zero.
_NON_NEGATIVE_TYPES = {
    AccountType.ESCROW_LIABILITY,
    AccountType.PROVIDER_PAYABLE,
    AccountType.REFUND_PAYABLE,
}


def post_transaction(
    description: str,
    entries: list[tuple[LedgerAccount, int]],
    idempotency_key: str | None = None,
    voucher_id: int | None = None,
) -> LedgerTransaction:
    """Atomically post a balanced transaction.

    `entries` is a list of (account, signed_amount_cents). The amounts must sum
    to zero. On idempotency-key replay, the original transaction is returned
    unchanged and nothing is posted.
    """
    if idempotency_key:
        existing = LedgerTransaction.query.filter_by(
            idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing

    if len(entries) < 2:
        raise LedgerError("A transaction needs at least two entries.")
    if any(amount == 0 for _, amount in entries):
        raise LedgerError("Zero-amount entries are not allowed.")
    if sum(amount for _, amount in entries) != 0:
        raise LedgerError("Transaction entries must sum to zero.")

    # Guard: under our sign convention, liability accounts are <= 0 while owed
    # (they grow negative when funded, return toward zero when released).
    # A posting that pushes them ABOVE zero would release more money than is
    # actually held — the classic escrow overdraw — so it must be rejected.
    for account, amount in entries:
        if account.type in _NON_NEGATIVE_TYPES:
            if account_balance(account) + amount > 0:
                raise LedgerError(
                    f"Posting would overdraw {account.type.value}:{account.reference}"
                )

    txn = LedgerTransaction(
        description=description,
        idempotency_key=idempotency_key,
        voucher_id=voucher_id,
    )
    db.session.add(txn)
    db.session.flush()
    for account, amount in entries:
        db.session.add(
            LedgerEntry(transaction_id=txn.id, account_id=account.id, amount_cents=amount)
        )
    db.session.flush()
    return txn
