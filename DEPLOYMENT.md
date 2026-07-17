# WalPay — Deployment & Maintenance Guide

## Recommended topology (launch scale)

One Docker container (gunicorn, 2 workers) + managed PostgreSQL, hosted on
Render/Fly.io/Railway — never on infrastructure inside South Sudan; clinics
and patients only need SMS and (optionally) a browser. Put the platform's
CDN/proxy (Cloudflare free tier is fine) in front for TLS, caching of the
tiny static pages, and rate limiting.

## First deployment

1. Provision PostgreSQL; set `DATABASE_URL`.
2. Set secrets (see table below). Generate strong values:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`
3. Run migrations: `flask --app app db upgrade -d migrations`
4. Create your admin: `flask --app app create-admin you@example.com`
   — scan the printed otpauth URI into your authenticator immediately;
   it is shown once.
5. Seed your first verified provider + bundles + portal user (SQL or shell).
6. Configure the Africa's Talking delivery callback URL:
   `https://<host>/api/sms/delivery-report?token=<AT_CALLBACK_TOKEN>`
7. Configure the PSP webhook to `https://<host>/api/webhooks/payments`.
8. Set `PSP_CHECKOUT_MODE=redirect` in production (disables the simulator).

## Required environment variables (production)

| Var | Notes |
|-----|-------|
| DATABASE_URL | postgres://... |
| SECRET_KEY | 48+ random bytes |
| VOUCHER_CODE_PEPPER | 48+ random bytes. Back up out-of-band: losing it invalidates all unredeemed codes. Never rotate casually. |
| PSP_WEBHOOK_SECRET | from the PSP dashboard |
| SMS_GATEWAY | `africastalking` |
| AT_API_KEY / AT_USERNAME / AT_CALLBACK_TOKEN | Africa's Talking |
| PSP_CHECKOUT_MODE | `redirect` |
| COOKIE_SECURE | leave unset (defaults on) |
| FEE_PERCENT | default 6 |

## Cron / scheduled jobs

```
*/1 * * * *  flask --app app sms-worker         # deliver queued SMS
0   2 * * *  flask --app app expire-vouchers    # expiry sweep + refund booking
0   9 * * 1  flask --app app export-payouts     # weekly payout CSV
0  10 * * *  flask --app app sample-callbacks   # anti-fraud sampling
```

## Proxy rate limits (set at Cloudflare/nginx)

- `/portal/login`, `/admin/login`: 10/min/IP
- `/api/redemption/redeem`, `/voucher/status`: 20/min/IP
- `/api/webhooks/*`: allow-list the PSP's published IPs if available

## Monitoring — alert on these

- `/healthz` non-200 (uptime probe)
- Reconciliation: trial balance non-zero or cash < obligations (check the
  admin dashboard daily; wire a scripted check later)
- SMS outbox: any message in FAILED status; QUEUED older than 1 hour
- Webhook 401s (secret mismatch) or 409s (money for unavailable bundle —
  needs manual refund)
- Log lines at ERROR level (structured JSON; ship to any log drain)

## Routine maintenance

- **Weekly**: export-payouts → pay via m-Gurush → confirm-payouts with the
  reference. Work the callback queue by phone before paying a provider's
  first three batches.
- **Monthly**: reconcile PSP settlement reports against platform_cash;
  restore-test a database backup; review locked portal/admin accounts.
- **Rollback strategy**: deploys are stateless — roll back by redeploying the
  previous image. Migrations are additive-only by policy; never drop or
  rewrite columns in the same release that stops writing them.

## Backup policy

Managed Postgres daily snapshots + point-in-time recovery if available. The
ledger is append-only: any restore must be to a point in time, never a
partial table restore, or the trial balance breaks.

## Incident basics

If the trial balance is ever non-zero: stop payouts (don't confirm batches),
export the ledger, find the unbalanced transaction by summing per
transaction_id — the invariant tests make this nearly impossible via the app,
so suspect manual SQL or a restore issue.
