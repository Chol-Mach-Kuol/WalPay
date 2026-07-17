"""Ledger invariants: if these pass, the business survives most other bugs."""
import pytest
from sqlalchemy import func

from app.extensions import db as sa_db
from app.models import AccountType, LedgerEntry
from app.services.ledger import (
    LedgerError,
    account_balance,
    get_or_create_account,
    post_transaction,
)


def _global_sum():
    return sa_db.session.query(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).scalar()


def test_balanced_transaction_posts(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, "voucher:1")
    post_transaction("purchase", [(cash, 1500), (escrow, -1500)])
    assert account_balance(cash) == 1500
    assert account_balance(escrow) == -1500
    assert _global_sum() == 0


def test_unbalanced_transaction_rejected(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, "voucher:2")
    with pytest.raises(LedgerError):
        post_transaction("bad", [(cash, 1500), (escrow, -1400)])
    assert _global_sum() == 0


def test_single_entry_rejected(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    with pytest.raises(LedgerError):
        post_transaction("bad", [(cash, 100)])


def test_zero_amount_entry_rejected(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, "voucher:3")
    with pytest.raises(LedgerError):
        post_transaction("bad", [(cash, 0), (escrow, 0)])


def test_escrow_cannot_be_overdrawn(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, "voucher:4")
    post_transaction("fund", [(cash, 1000), (escrow, -1000)])
    # Releasing more than escrowed must fail.
    payable = get_or_create_account(AccountType.PROVIDER_PAYABLE, "provider:1")
    with pytest.raises(LedgerError):
        post_transaction("overdraw", [(escrow, 1500), (payable, -1500)])


def test_idempotency_key_prevents_double_posting(db):
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    escrow = get_or_create_account(AccountType.ESCROW_LIABILITY, "voucher:5")
    t1 = post_transaction("purchase", [(cash, 900), (escrow, -900)], idempotency_key="pay:abc")
    t2 = post_transaction("purchase", [(cash, 900), (escrow, -900)], idempotency_key="pay:abc")
    assert t1.id == t2.id
    assert account_balance(cash) == 900  # posted exactly once
