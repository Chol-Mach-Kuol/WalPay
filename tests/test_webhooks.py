"""Webhook ingestion: signature checks and replay safety."""
from app.models import AccountType, Voucher
from app.services.ledger import account_balance, get_or_create_account


def _payload(seed, event_id="evt_1"):
    return {
        "event_id": event_id,
        "event": "charge.completed",
        "source": "flutterwave",
        "data": {
            "sender_id": seed["sender"].id,
            "bundle_id": seed["bundle"].id,
            "recipient_phone": "+211911111111",
            "fee_cents": 100,
        },
    }


HEADERS = {"verif-hash": "test-webhook-secret"}


def test_unsigned_webhook_rejected(client, seed):
    resp = client.post("/api/webhooks/payments", json=_payload(seed))
    assert resp.status_code == 401
    assert Voucher.query.count() == 0


def test_signed_webhook_issues_voucher(client, db, seed):
    resp = client.post("/api/webhooks/payments", json=_payload(seed), headers=HEADERS)
    assert resp.status_code == 201
    assert Voucher.query.count() == 1


def test_replayed_webhook_is_ignored(client, db, seed):
    client.post("/api/webhooks/payments", json=_payload(seed), headers=HEADERS)
    resp = client.post("/api/webhooks/payments", json=_payload(seed), headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "duplicate_ignored"
    assert Voucher.query.count() == 1
    cash = get_or_create_account(AccountType.PLATFORM_CASH)
    assert account_balance(cash) == 1600  # charged exactly once


def test_malformed_payload_rejected(client, seed):
    resp = client.post("/api/webhooks/payments", json={"nope": 1}, headers=HEADERS)
    assert resp.status_code == 400


def test_irrelevant_event_acknowledged_without_voucher(client, seed):
    payload = _payload(seed, event_id="evt_other")
    payload["event"] = "charge.failed"
    resp = client.post("/api/webhooks/payments", json=payload, headers=HEADERS)
    assert resp.status_code == 200
    assert Voucher.query.count() == 0
