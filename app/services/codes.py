"""Redemption code generation and verification.

Security model:
- Codes are 8 characters from an unambiguous alphabet (no 0/O/1/I), giving
  ~40 bits of entropy — combined with per-voucher phone binding and a
  5-attempt lockout, online brute force is impractical.
- Only a peppered HMAC-SHA256 of the code is stored. A database leak alone
  does not allow forging redemptions; the pepper lives only in the app env.
- Comparison uses constant-time equality.
"""
import hashlib
import hmac
import secrets

from flask import current_app

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 8


def generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def hash_code(code: str) -> str:
    pepper = current_app.config["VOUCHER_CODE_PEPPER"].encode()
    normalized = code.strip().upper().encode()
    return hmac.new(pepper, normalized, hashlib.sha256).hexdigest()


def verify_code(candidate: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_code(candidate), stored_hash)
