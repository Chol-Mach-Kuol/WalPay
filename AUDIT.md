# Production Readiness Audit — WalPay v1

Date: 2026-07-17 · Scope: full codebase, phases 1–4 · Method: automated
sweeps + 51-test suite + end-to-end smoke tests (happy path and fraud path).

## Verified ✅

**Money correctness** — append-only double-entry ledger, integer cents only
(sweep confirmed no float arithmetic in money paths), balanced-transaction and
escrow-overdraw invariants enforced and tested, idempotency keys on every
posting path (webhook replay, payout re-confirm, dispute re-resolve all
proven no-ops). Trial balance holds through every tested lifecycle including
the dispute-clawback path.

**OWASP top 10 coverage** — SQL injection: ORM-only (sweep: no raw SQL).
XSS: Jinja autoescaping, no |safe anywhere. CSRF: per-session tokens on all
state-changing POSTs, constant-time compare. Auth: scrypt hashes, lockouts,
timing-equalized unknown-user paths, session rotation on login, TOTP on
admin. Sessions: HttpOnly + SameSite=Lax + Secure default. Secrets: env-only
(sweep clean), and the app now REFUSES TO BOOT in production with dev
fallbacks. SSRF/command injection: no user-controlled URLs fetched, no shell
execution.

**Resilience** — SMS transactional outbox survives total gateway outage
(tested); webhook ingestion is exactly-once; correlation-ID structured
logging; health endpoint; stateless containers with additive-only migration
policy for rollback.

**Access & UX floor** — labels on all inputs, visible focus outlines,
reduced-motion respected, ~5 KB pages, mobile-first.

## Accepted risks / open items for launch 🟡

1. **Rate limiting is proxy-level, not in-app** — MUST configure per
   DEPLOYMENT.md before launch. The in-app per-voucher/per-account lockouts
   are the second layer, not the first.
2. **PSP redirect is not wired** — checkout stops at the intent stage until
   Flutterwave/Pesapal credentials exist. The webhook side is complete.
   Confirm merchant-of-record status (money-transmitter exposure) in writing.
3. **Refund execution is manual** — refunds are booked as liabilities;
   actually returning money is an ops task until PSP refund API wiring.
4. **Provider/portal user creation is CLI/shell** — acceptable below ~20
   providers; build admin CRUD before scaling onboarding.
5. **Single-region DB** — fine at launch scale; enable PITR backups day one.
6. **Load testing not performed** — architecture (indexed queries, tiny
   pages, 2 workers) is comfortable for thousands of users/day, but verify
   with k6 or Locust before any marketing push.

## Verdict

Safe to pilot with real money at small scale (a handful of providers, manual
weekly payouts, callback verification on early batches) once items 1–2 are
closed. Not yet ready for unattended scale — close items 3–4 first.
