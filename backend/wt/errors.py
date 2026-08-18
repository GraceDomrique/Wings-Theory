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
