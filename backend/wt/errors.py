"""Domain errors.

Each carries enough structure for the API layer to turn it into a useful
response without string-matching on the message.
"""
from __future__ import annotations


class WTError(Exception):
    """Base. `code` is stable and safe to branch on; `message` is for humans."""
    code = "wt_error"
    http_status = 400

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class UnitConversionError(WTError):
    code = "unit_conversion"
    http_status = 422


class UnmappedIngredientError(WTError):
    """A recipe line has no resolved ingredient_id. Never raised from the write
    path — the schema blocks that — only from import and validation."""
    code = "unmapped_ingredient"
    http_status = 422


class RecipeCycleError(WTError):
    """A sub-recipe expands into itself."""
    code = "recipe_cycle"
    http_status = 422


class NotSellableError(WTError):
    """The menu-completeness gate rejected this item."""
    code = "not_sellable"
    http_status = 409


class InsufficientStockError(WTError):
    """Reservation failed: available (on_hand - reserved) was below what the
    order needed. Carries the specific shortfall so the POS can name it."""
    code = "insufficient_stock"
    http_status = 409

    def __init__(self, message: str, shortfalls: list[dict] | None = None):
        super().__init__(message, shortfalls=shortfalls or [])
        self.shortfalls = shortfalls or []


class InvalidStateError(WTError):
    """An order or reservation transition that the state machine forbids."""
    code = "invalid_state"
    http_status = 409


class NotFoundError(WTError):
    code = "not_found"
    http_status = 404


class PermissionError_(WTError):
    code = "forbidden"
    http_status = 403


class ValidationError(WTError):
    """Input failed validation before it ever reached a write -- a bad
    verification value, a weak password, a malformed request field. The
    boundary that catches ValueError/TypeError/sqlite3 errors and raises
    this instead is responsible for making sure `message` is safe to show
    a user directly; it must never carry a raw exception string or a SQL
    fragment."""
    code = "validation_error"
    http_status = 422


class PasswordChangeRequiredError(WTError):
    """The account is flagged must_change_password=1 (fresh seed or an admin
    reset) and is trying to reach a capability-gated endpoint before setting
    its own password. Deliberately distinct from PermissionError_ (403 with
    a different `code`) so a client can special-case this into "set a new
    password" UI instead of a generic "not allowed" message. /auth/login and
    /auth/change_password never go through needs(), so this can never block
    the one action that clears it."""
    code = "password_change_required"
    http_status = 403


class ModelRegistryUnavailableError(WTError):
    """ml_model_registry/ml_training_run do not exist yet in this database
    (migrations not applied -- e.g. a fresh checkout before db.migrate()
    has run). Deliberately distinct from NotFoundError (registry exists,
    row doesn't) and from a raw sqlite3 error on a genuinely corrupted
    schema, which must still propagate unmasked -- this class covers only
    the specific, recoverable-by-migrating "no such table" condition."""
    code = "model_registry_unavailable"
    http_status = 503


class AuthenticationError(WTError):
    """The caller could not be identified at all — no token, expired token,
    malformed token, bad signature.

    Deliberately separate from PermissionError_ (403). Conflating the two is
    not a semantic nitpick: every client treats 401 as "your session ended,
    sign in again" and 403 as "you are signed in but not allowed here". When
    an expired token came back as 403, the console stayed looking logged-in
    and every request failed forever with no way out except clearing storage
    by hand — the shift-long dead screen this class exists to prevent.
    """
    code = "unauthenticated"
    http_status = 401
