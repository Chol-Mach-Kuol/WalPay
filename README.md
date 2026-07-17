# WalPay — Phases 1–4 (complete v1)

Escrow-backed, single-use health vouchers: diaspora senders prepay care at
verified South Sudanese clinics/pharmacies; patients redeem via an 8-character
code bound to their phone number; funds release from escrow only on redemption.

**Deliberate constraints:** all money is integer USD cents (no floats, no SSP
in the ledger); no medical data is stored anywhere (bundles are commercial
titles, never diagnoses); vouchers are single-use in v1.

## Architecture

Modular Flask monolith:

```
app/
  __init__.py          # app factory, correlation-ID logging, security headers
  models.py            # Sender, Provider, ServiceBundle, Voucher, ledger, webhooks
  services/
    ledger.py          # append-only double-entry ledger with invariants
    vouchers.py        # state machine + escrow postings (atomic together)
    codes.py           # peppered-HMAC redemption codes
  api/
    webhooks.py        # PSP webhook: signature check + idempotent ingestion
    redemption.py      # provider redemption endpoint (API-key auth in v1)
tests/                 # 18 tests: ledger invariants, lifecycle, replay safety
```

## Money flow (signed entries sum to zero per transaction)

| Event    | platform_cash | escrow_liability | provider_payable | fee_revenue | refund_payable |
|----------|--------------|------------------|------------------|-------------|----------------|
| Purchase | +value+fee   | −value           |                  | −fee        |                |
| Redeem   |              | +value           | −value           |             |                |
| Payout   | −value       |                  | +value           |             |                |
| Expire   |              | +value           |                  |             | −value         |

Liability accounts are negative while owed; the ledger rejects any posting
that would push them above zero (escrow overdraw protection).

## Voucher state machine

`issued → redeemed → paid_out`, with `issued → expired|refunded` and
`redeemed → disputed → paid_out|refunded`. Illegal transitions raise
`VoucherError("illegal_transition")` and post nothing.

## Security summary

- Redemption codes: 8 chars, ~40-bit entropy, unambiguous alphabet; only a
  peppered HMAC-SHA256 stored; constant-time comparison; phone binding;
  5-attempt lockout; generic error messages (no oracle for which field failed).
- Webhooks: shared-secret signature (fails closed if unset), unique
  (source, event_id) constraint makes replays no-ops.
- Idempotency keys on every ledger posting path.
- SQLAlchemy ORM throughout (parameterized — no SQL injection surface).
- Deploy note: add reverse-proxy rate limiting on /api/redemption and
  /api/webhooks; serve HTTPS only.

## Environment variables

| Var | Purpose |
|-----|---------|
| DATABASE_URL | PostgreSQL URL in prod (SQLite fallback for dev) |
| SECRET_KEY | Flask session signing |
| VOUCHER_CODE_PEPPER | HMAC pepper for redemption codes — rotate = old codes invalid; back up securely |
| PSP_WEBHOOK_SECRET | Shared secret from the payment provider |
| PROVIDER_API_KEYS | v1 keyring: `provider_id:sha256hex,...` |
| VOUCHER_EXPIRY_DAYS | Default 90 |
| REDEMPTION_MAX_ATTEMPTS | Default 5 |

## API (Phase 1)

- `POST /api/webhooks/payments` — PSP events; header `verif-hash`. 201 issues a
  voucher, 200 for duplicates/ignored events, 401 bad signature, 409 money
  arrived for an unavailable bundle (needs human review).
- `POST /api/redemption/redeem` — header `X-Provider-Key`; body
  `{code, recipient_phone}`. 200 redeemed, 422 invalid, 423 locked.
- `GET /healthz` — liveness.

## Run

```bash
pip install -r requirements.txt
python -m pytest tests/ -q        # 18 tests
flask --app app run               # dev server
```

## Phase 2 additions

- **SMS transactional outbox** (`services/sms.py`): messages are written in the
  same DB transaction as the business event; a worker delivers them through a
  pluggable gateway (Africa's Talking in prod, console in dev/test) with
  exponential backoff (1m/5m/25m/~2h/~10h) then FAILED for human follow-up.
  A total gateway outage delays codes; it never loses them.
- **Delivery reports**: `POST /api/sms/delivery-report?token=<AT_CALLBACK_TOKEN>`
  (configure in the AT dashboard). Carrier failures requeue once automatically.
- **Expiry sweep**: `flask expire-vouchers` books refunds and notifies patients.
- **Payout batches**: `flask export-payouts` emits a per-provider CSV;
  `flask confirm-payouts <batch_id> <reference>` transitions vouchers to
  PAID_OUT atomically with ledger postings. Re-running cannot double-pay.

Cron (every minute / nightly / weekly):
```
*/1 * * * *  flask --app app sms-worker
0   2 * * *  flask --app app expire-vouchers
0   9 * * 1  flask --app app export-payouts > payouts.csv
```

Additional env vars: `SMS_GATEWAY` (`africastalking` or `console`),
`AT_API_KEY`, `AT_USERNAME`, `AT_CALLBACK_TOKEN`.

## Phase 3 additions

- **Sender storefront** (`/`, `/checkout/<bundle>`, `/purchase/<id>`,
  `/voucher/status`): mobile-first, zero JS frameworks, zero webfonts, zero
  images — every page is ~5 KB and loads on a 2G connection. Checkout
  snapshots the price into a PurchaseIntent so mid-checkout price edits can't
  change what the sender pays. Dev mode includes a payment simulator
  (`PSP_CHECKOUT_MODE=dev`); prod redirects to the PSP hosted page (Phase 4
  wiring). The completed-purchase and status pages render the signature
  "voucher ticket" — a perforated receipt card; codes and money are always
  monospace.
- **Provider portal** (`/portal/...`): session login for clinic staff
  (scrypt-hashed passwords, 5-failure/15-minute lockout, timing-equalized
  unknown-email path, session rotation on login), a redemption form with
  human-readable outcomes, pending-payout total, and recent activity.
- **Web security**: per-session CSRF tokens on every POST, HttpOnly +
  SameSite=Lax (+ Secure by default) cookies, Jinja autoescaping, E.164 phone
  and email validation, generic errors that never reveal which field failed.

Additional env vars: `PSP_CHECKOUT_MODE` (`dev`|`redirect`), `FEE_PERCENT`
(default 6), `COOKIE_SECURE` (`0` only for local HTTP dev).

Create a portal user (until the admin UI ships in Phase 4):
```python
from app.web.security import hash_password
db.session.add(ProviderUser(provider_id=1, email="reception@clinic.example",
                            password_hash=hash_password("<strong password>")))
```

## Roadmap

- **Phase 4:** sender web UI (mobile-first, low-bandwidth), provider portal
  replacing API keys with sessions + TOTP.
- **Phase 4:** admin dashboard (disputes, random patient callback queue for
  fraud detection), reconciliation reports, deployment hardening.

## Known v1 limitations (deliberate)

Manual payouts (fraud visibility); env-var provider keyring (fine for <20
providers); no partial redemption; refund execution is an ops task booked in
`refund_payable`. Confirm marketplace/merchant-of-record status with your PSP
before launch — this determines your money-transmitter exposure.
