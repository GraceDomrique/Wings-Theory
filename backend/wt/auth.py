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

ROLES = ("admin", "manager", "cashier", "kitchen", "owner")

# Capability -> roles allowed.
#
# 2026-08 RBAC audit finding: 'admin' and 'manager' were IDENTICAL in every
# capability below except user.manage. The fix at the time made 'admin'
# structurally read-only-plus-sign-off, on the theory that the one account
# with no on-site supervisor should never unilaterally alter operational
# records. That produced its own problem in practice: the Administrator
# role is the one meant to hold full system authority (it already owns
# user.manage, the most sensitive capability there is), while 'manager' —
# reachable from a shared back-of-house login — was the one actually able
# to create, edit and delete inventory, recipes and prices.
#
# 2026-09 revision: 'admin' now holds every capability — full view, create,
# edit and delete access system-wide, consistent with it being the single
# fully-trusted role. 'manager' is narrowed to read/monitoring capabilities
# only: dashboards, sales, inventory, recipes, queues, forecasts and
# anomaly/consumption monitoring. No capability below grants 'manager' the
# ability to create, edit, delete, or otherwise mutate a record. Where a
# capability previously covered both a read and a write action for the same
# resource (viewing pending reconciliations vs. submitting one), it has been
# split so the read half can stay with 'manager' and the kitchen station
# that also depends on it, while the write half moves to 'admin' only. See
# TestRBAC in run_tests.py.
#
# 2026-09-11 addition: 'owner' is a genuinely new, separate role (not a
# relabeling of 'manager' — both exist, independently, going forward). It
# is read-only across every existing capability below, added only to rows
# 'manager' already held (order.read, menu.read, inventory.read,
# recipe.read) — never inventory.write, recipe.write, queue.resolve,
# unli.approve, user.manage, or permissions.manage. It gets exactly two new
# capabilities that exist nowhere else in this table, each a net-new
# workflow designed for it: instruction.create (an owner-to-admin task, see
# wt/instructions.py) and purchase_order.create (a draft purchase order,
# see wt/purchasing.py). Nothing here weakens 'admin' or 'manager'.
CAPABILITIES = {
    "order.create":       ("cashier",),
    # Kitchen can void too, but only ever reaches a 'punched' (not-yet-
    # started) ticket from the KDS UI — releasing a hold with zero ledger
    # impact. Voiding an order already in progress still requires walking
    # back to the till; that path posts a real compensating wastage entry,
    # which is a create/edit/delete-class action reserved for the till
    # itself, the kitchen's own zero-impact case, or the admin's full
    # access — never the manager's read-only role.
    "order.void":         ("cashier", "admin", "kitchen"),
    # 2026-10-14 fix: recording the printed "Leftover Charge" penalty fee
    # (migration 0037's customer_order.leftover_charge_total) used to reuse
    # order.void, on the reasoning that both are "post-checkout order
    # adjustments" -- but that coupling is wrong: order.void includes
    # 'kitchen' for a completely different, narrow reason (releasing a
    # still-punched, not-yet-started ticket from the KDS screen, with zero
    # billing impact), and kitchen has no other billing-facing capability
    # anywhere in this table. Reusing order.void would have quietly let
    # kitchen staff record a billing charge nobody intended them to. A
    # leftover charge is purely a till/billing action, assessed once a
    # table's bill is already totaled (see wt/reservation.
    # set_leftover_charge()) -- scoped to exactly the roles that handle
    # checkout and billing, same set as order.create, plus admin for the
    # same oversight reason every other write capability lists it.
    "order.leftover_charge": ("cashier", "admin"),
    "order.status":       ("kitchen",),
    "order.read":         ("cashier", "manager", "admin", "owner"),
    "menu.read":          ("cashier", "kitchen", "manager", "admin", "owner"),
    "inventory.read":     ("manager", "admin", "owner"),
    "inventory.write":    ("admin",),
    # 2026-10 audit: inventory.write alone meant the only person on-site who
    # could correct a physical stock count was 'admin' — a back-office role,
    # not whoever is actually standing in front of the shortage. This is
    # deliberately narrow: it grants ONLY what POST /console/ingredients/
    # {pk}/count and /bulk-count need (submit a physical count, see the
    # current expected quantity, optionally add a note) — never receive,
    # waste/spoilage, manual use, recipe/ingredient editing, or purchasing,
    # all of which stay inventory.write-only exactly as before. 'kitchen' is
    # the station that actually discovers a shortage mid-service (see
    # kds.html's Correct Stock sheet) and already holds one other narrow
    # operational-write capability with the same shape (unli.reconcile.write,
    # just above order.void). 'admin' is listed explicitly for the same
    # reason order.void lists every role that can reach it directly, rather
    # than relying on inventory.write's separate role list to cover it.
    #
    # Deliberately NOT granted to 'manager' or 'owner' despite both being
    # plausible-sounding candidates: 'manager' is a retired role (migration
    # 0034 deactivated the one seeded account; create_user refuses it for
    # new accounts — see this file's own CAPABILITIES comment above and
    # api.py's create_user) and granting it a capability no live account can
    # use is dead configuration, not a safeguard. 'owner' is read-only by
    # explicit design everywhere in this table (instruction.create and
    # purchase_order.create are the only two exceptions, and both are
    # owner-to-admin workflows, not a direct operational mutation) — adding
    # inventory write access of any shape here would be the first capability
    # to break that invariant. Not granted to 'cashier' either: the POS, not
    # the till, is where a shortage is discovered, and nothing in this phase
    # demonstrated a cashier-specific need for it.
    "inventory.correct_stock": ("kitchen", "admin"),
    "recipe.read":        ("manager", "admin", "owner"),
    "recipe.write":       ("admin",),
    # Whether a service charge applies at all, and which of the
    # Senior/PWD/Staff discount cards a cashier may select — a business
    # decision, admin-only like every other write capability. Reading the
    # effective result is menu.read (GET /pos/menu), same as every other
    # POS-preview figure; this only gates changing it.
    "pos_config.write":   ("admin",),
    "queue.read":         ("manager", "admin"),
    "queue.resolve":      ("admin",),
    # Viewing pending/completed reconciliations and the coefficients they
    # feed — a monitoring capability, so the manager keeps it, alongside the
    # kitchen station that reads its own pending list from the KDS. Actually
    # *submitting* a reconciliation is unli.reconcile.write, just below.
    "unli.reconcile":     ("kitchen", "manager", "admin"),
    # Recording the measurement is a create action against a real ledger
    # entry (it posts a stock correction). The kitchen is the only station
    # that can physically weigh the tray, so it keeps this; the manager,
    # who cannot, does not.
    "unli.reconcile.write": ("kitchen", "admin"),
    # Signing off a day that fell outside tolerance, and recalibrating a
    # coefficient off the validated history, both edit records that change
    # what the POS will deduct tomorrow — admin-only, like every other
    # write capability.
    "unli.approve":       ("admin",),
    # Recording a round of unlimited wings, opening/closing a table's
    # session. Floor stations create these records as part of taking and
    # serving an order; the manager's role is monitoring, not floor
    # execution, so it holds neither. Admin gains both so an administrator
    # signed into POS/KDS (see ALLOWED_ROLES on those pages) has full,
    # working access rather than a capability gap.
    "unli.refill":        ("cashier", "kitchen", "admin"),
    "unli.session":       ("cashier", "kitchen", "admin"),
    "user.manage":        ("admin",),
    # Configuring which console PAGES a role can reach at all (distinct from
    # every capability above, which governs an individual action). See
    # wt/permissions.py.
    "permissions.manage": ("admin",),

    # --- 2026-09-11: Owner instructions and purchase orders --------------
    # Creating an instruction/draft PO, and cancelling one's own while it is
    # still in its initial state ('open'/'draft') — enforced inside
    # wt/instructions.py and wt/purchasing.py by checking created_by against
    # the caller, not by a separate capability per branch (same pattern
    # order.void already uses). Every other transition — acknowledge, start,
    # complete, mark ordered, receive, admin-side cancel — is admin-only:
    # OWNER never gains write access to the operational system itself.
    "instruction.create":      ("owner",),
    "instruction.update_status": ("admin",),
    # Both roles can reach the cancel endpoint; which instruction and from
    # which status is a finer rule enforced inside wt/instructions.cancel()
    # (owner: only their own, only from 'open'; admin: from any cancellable
    # status) — same shape as order.void below, whose capability also lists
    # every role that can reach the route and leaves the per-branch rule to
    # the function itself.
    "instruction.cancel":       ("owner", "admin"),
    "instruction.read":         ("owner", "admin"),
    "purchase_order.create":    ("owner",),
    "purchase_order.manage":    ("admin",),
    "purchase_order.cancel":    ("owner", "admin"),
    "purchase_order.read":      ("owner", "admin"),

    # Read-only visibility into admin_audit_log/login_event (2026-10
    # accountability pass). Deliberately separate from user.manage, which
    # stays admin-only for actually administering accounts — owner needs to
    # SEE who touched what and when (that is the entire point of giving
    # admin operational write access while owner retains oversight), not to
    # manage users. Granting this never widens any write capability above.
    "audit.read":         ("owner", "admin"),

    # Saving a "combo idea" (wt/combo_pricing.py) as a draft, and reviewing/
    # approving that draft — a pricing decision, same owner/admin pairing as
    # purchase_order.cancel above. Never publishes to the live menu/POS;
    # see wt/api.py's draft-combo endpoints.
    "combo.draft":        ("owner", "admin"),

    # Publishing a combo to the live menu/POS creates a real menu_item +
    # recipe — an operational write exactly like recipe.write/inventory.write,
    # not a pricing decision like combo.draft above. Admin-only: owner stays
    # read-only (approving the IDEA is combo.draft; making it sellable is
    # this), consistent with every other write capability in this table.
    "combo.publish":      ("admin",),

    # 2026-10 Phase 2 audit finding: GET /console/dashboard/owner-overview
    # and GET /console/sales/owner were gated on inventory.read, same as
    # every ingredient/stock-reading endpoint — which also grants 'admin'
    # (inventory.read lists manager/admin/owner, see above). That is correct
    # for inventory data itself, but these two routes are not inventory
    # reads: they are the Owner-role strategic business-intelligence
    # aggregation (category/customer trend analysis, growth drivers,
    # business rhythm, FP-Growth associations framed as revenue strategy) —
    # a distinct, Owner-only screen the frontend's own nav tree
    # (OWNER_TREE's Business Performance group) never shows an admin
    # account, and the console's role split throughout this file otherwise
    # keeps that content owner-side (instruction.create, purchase_order.
    # create above). Nav hiding it is not authorization: an admin token
    # could call either route directly. Scoped to owner only — nothing an
    # admin legitimately needs lives behind this capability; Admin Today and
    # Admin Sales (the operational counterparts) stay on inventory.read,
    # unchanged, since admin's own operational screens were never the gap.
    "dashboard.owner_intelligence": ("owner",),
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
    defeats the point of an emergency reset.

    'jti' is a fresh random id for THIS token, independent of anything else
    in the payload. It exists so a single session can be individually
    revoked (see wt/sessions.py and api.py's POST /auth/logout) without
    touching any other token the same account holds -- the 'pwd' claim
    above already invalidates *every* token on a password change, but there
    was no equivalent for "just this one, right now, because the user
    signed out." Callers that need the session recorded (api.py's login()
    and change_password()) decode the returned token straight back with
    decode_token() to read this claim and the 'exp' above -- issue_token
    itself never touches the database, consistent with every other
    function in this module."""
    if role not in ROLES:
        raise WTError(f"unknown role {role!r}")
    header = {"alg": ALGO, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": username, "role": role, "iat": now, "exp": now + ttl,
        "pwd": password_changed_at, "jti": secrets.token_hex(16),
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
