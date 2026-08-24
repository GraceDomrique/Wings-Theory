"""Role-scoped auth.

HS256 JWT written against the stdlib rather than pulling in python-jose. It is
about forty lines, removes a dependency from a machine that sits in a shop
back room, and there is nothing exotic here worth outsourcing.

Passwords use PBKDF2-HMAC-SHA256. Not argon2 — that would mean a compiled
dependency on a Windows mini-PC for four staff accounts on a LAN. 240k
iterations is well above the practical threshold for this threat model.

Threat model, stated plainly: the backend is not reachable from the internet.
What auth defends against here is a cashier tablet being used to reach the
owner's console, and a lost device being used later. It is not defending a
public endpoint.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import time

from .errors import AuthenticationError, PermissionError_, ValidationError, WTError

ALGO = "HS256"
TOKEN_TTL = 12 * 3600          # one shift plus overtime
ITERATIONS = 240_000

# The fallback signing key when WT_SECRET isn't set. Named so a production
# startup check (db.validate_production_startup) can refuse to run with it
# still in effect, instead of duplicating the literal in two files.
DEFAULT_SECRET = "dev-only-change-in-production"

# The shared seed password every account was created with (seed.py
# seed_users). Rejected outright as a *new* password on either the
# self-service change or the admin reset path -- a reset that could put the
# very credential this phase exists to retire back in place would defeat
# the point of having a reset endpoint at all.
KNOWN_COMPROMISED_PASSWORDS = {"wingstheory"}
MIN_PASSWORD_LENGTH = 10

# Login lockout. Not defending a public endpoint (see module docstring) --
# defending a lost/compromised device and unlimited guesses against the
# still-active shared seed password until an owner rotates it.
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

ROLES = ("admin", "manager", "cashier", "kitchen")

# Capability -> roles allowed. Mirrors the access matrix in the build document.
CAPABILITIES = {
    "order.create":       ("cashier", "admin"),
    # Kitchen can void too, but only ever reaches a 'punched' (not-yet-
    # started) ticket from the KDS UI — releasing a hold with zero ledger
    # impact. Voiding an order already in progress still requires walking
    # back to the till; that path posts a real compensating wastage entry
    # and stays a cashier/manager/admin decision.
    "order.void":         ("cashier", "manager", "kitchen", "admin"),
    "order.status":       ("kitchen", "admin"),
    "order.read":         ("cashier", "manager", "admin"),
    "menu.read":          ("cashier", "kitchen", "manager", "admin"),
    "inventory.read":     ("manager", "admin"),
    "inventory.write":    ("manager", "admin"),
    "recipe.read":        ("manager", "admin"),
    "recipe.write":       ("manager", "admin"),
    "queue.read":         ("manager", "admin"),
    "queue.resolve":      ("manager", "admin"),
    # Narrowly scoped on purpose. Reconciling an unlimited stream is a kitchen
    # job — the kitchen is the only station that can weigh the tray — but it
    # is the ONLY inventory-affecting thing the kitchen may do. Granting
    # inventory.write to make it work would also hand a kitchen tablet stock
    # receiving, write-offs, physical counts and every ingredient field in the
    # console, none of which it has any business touching.
    "unli.reconcile":     ("kitchen", "manager", "admin"),
    # Signing off a day that fell outside tolerance, and recalibrating a
    # coefficient off the validated history, both change what the POS will
    # deduct tomorrow. That is a management decision, not a line one.
    "unli.approve":       ("manager", "admin"),
    # Recording a round of unlimited wings. Both stations legitimately do this
    # — the cashier takes the table's flavour, the kitchen serves it — so both
    # hold it. It grants nothing beyond refills: the sauce deduction it
    # triggers is sized by the server from the session's own head count, so
    # holding this capability is not a way to move arbitrary stock.
    "unli.refill":        ("cashier", "kitchen", "manager", "admin"),
    # Closing or cancelling a table's session. The floor owns this; reopening
    # a closed one is deliberately separate (unli.approve) so a session cannot
    # be resurrected by the same accidental tap that ended it.
    "unli.session":       ("cashier", "kitchen", "manager", "admin"),
    "user.manage":        ("admin",),
}


def _secret() -> bytes:
    return os.environ.get("WT_SECRET", DEFAULT_SECRET).encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def validate_new_password(password: str, *, username: str | None = None) -> None:
    """Rejects a proposed password before it is ever hashed or written.
    Used by both the self-service change and the admin reset path (the
    latter only for defense in depth -- generate_temp_password() below
    already produces something that passes this on its own)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if password.lower() in KNOWN_COMPROMISED_PASSWORDS:
        raise ValidationError(
            "that password is a known shared/compromised default and cannot be used"
        )
    if username and password.lower() == username.lower():
        raise ValidationError("password cannot be the same as the username")


def generate_temp_password(length: int = 16) -> str:
    """A cryptographically random password for the admin-reset path -- the
    admin never types (and therefore never has to invent, remember, or
    reuse) the credential a reset hands back."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------
def issue_token(username: str, role: str, password_changed_at: str, ttl: int = TOKEN_TTL) -> str:
    """password_changed_at is embedded as the 'pwd' claim so a token minted
    before a password change can be told apart, at request time, from one
    minted after it -- see api.principal(), which compares this claim
    against app_user's current column value and rejects a mismatch. Without
    this, changing or resetting a password would leave every token already
    issued for that account valid until its own 12-hour expiry, which
    defeats the point of an emergency reset."""
    if role not in ROLES:
        raise WTError(f"unknown role {role!r}")
    header = {"alg": ALGO, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": username, "role": role, "iat": now, "exp": now + ttl,
        "pwd": password_changed_at,
    }
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = hmac.new(_secret(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


def decode_token(token: str) -> dict:
    """Identity check only — 401 on every failure, never 403.

    A token problem means "we don't know who you are", which is a different
    answer from "we know who you are and you may not do that". Clients key
    their sign-in-again behaviour off 401; returning 403 here left an expired
    session stuck in a permanently failing UI.
    """
    try:
        h_b64, p_b64, sig_b64 = token.split(".")
    except ValueError:
        raise AuthenticationError("malformed token")

    # A token that is the right *shape* but not valid base64/JSON is still
    # just an unusable credential. Decoding it raised binascii/JSON errors
    # that escaped as HTTP 500s, so a single corrupted localStorage entry
    # crashed the endpoint instead of asking the user to sign in again.
    try:
        expected = hmac.new(_secret(), f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
        signature_ok = hmac.compare_digest(expected, _unb64(sig_b64))
    except Exception:
        raise AuthenticationError("malformed token")
    if not signature_ok:
        raise AuthenticationError("bad token signature")

    try:
        payload = json.loads(_unb64(p_b64))
    except Exception:
        raise AuthenticationError("malformed token")
    if not isinstance(payload, dict):
        raise AuthenticationError("malformed token")

    if payload.get("exp", 0) < time.time():
        raise AuthenticationError("session expired — please sign in again")
    return payload


def require(payload: dict, capability: str) -> None:
    allowed = CAPABILITIES.get(capability)
    if allowed is None:
        raise WTError(f"unknown capability {capability!r}")
    if payload.get("role") not in allowed:
        raise PermissionError_(
            f"role '{payload.get('role')}' cannot {capability}",
            capability=capability,
        )
