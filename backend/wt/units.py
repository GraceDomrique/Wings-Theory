"""Purchase-unit to base-unit conversion, and quantity parsing for import.

The whole point of this module is that exactly one place in the system knows how
to turn "4 packs" into grams, and it refuses to guess. Everything downstream —
deduction, days-of-supply, forecast, expiry risk — reads base units only.

Conversion
----------
    base_qty = purchase_qty * pack_size * yield_factor

pack_size answers "how much is in one pack". yield_factor answers "how much of
that survives preparation" — drain loss on canned tuna, trim on garlic, or the
weight *gain* when raw rice is steamed (a factor above 1.0).

Both are per-ingredient and both carry a verification status, because an
unconfirmed 1.0 yield is a guess wearing the costume of a fact.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .errors import UnitConversionError

BASE_UNITS = ("g", "ml", "pc")

# Only conversions inside one measurement family are automatic. There is no
# g <-> ml entry: that needs a density, which is per-ingredient, not universal.
_SCALE = {
    ("kg", "g"): 1000.0,
    ("g", "g"): 1.0,
    ("mg", "g"): 0.001,
    ("l", "ml"): 1000.0,
    ("ml", "ml"): 1.0,
    ("cl", "ml"): 10.0,
    ("pc", "pc"): 1.0,
}

_UNIT_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kls": "kg", "kilogram": "kg",
    "g": "g", "gram": "g", "grams": "g", "gr": "g",
    "mg": "mg",
    "l": "l", "li": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "ml": "ml", "milliliter": "ml", "millilitre": "ml",
    "cl": "cl",
    "pc": "pc", "pcs": "pc", "piece": "pc", "pieces": "pc", "ea": "pc", "each": "pc",
}


def normalise_label(raw: str) -> str:
    """Canonical form of an ingredient label, used only for alias lookup during
    import. Never used to *decide* a mapping — a human does that once, and the
    result is stored in ingredient_alias."""
    s = unicodedata.normalize("NFKD", raw or "")
    s = s.encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\([^)]*\)", " ", s)          # drop parentheticals
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass(frozen=True)
class ParsedQty:
    qty: float
    unit: str            # normalised: kg/g/mg/l/ml/cl/pc, or "" when unknown
    raw: str
    ambiguous: bool = False


_QTY_RE = re.compile(
    r"^\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?:/\s*\d+(?:\.\d+)?\s*)?([a-zA-Z]*)", re.UNICODE
)


def parse_quantity(text: str) -> ParsedQty:
    """Parse a spreadsheet quantity cell such as '120 g', '2pcs', '35-40 g',
    '8 g (2 cloves)', '1/2 pc' or '1,000 g'.

    Comma is treated as a thousands separator (matched only in properly
    grouped triples: '1,000', '12,345') and period as the decimal point —
    this dataset is US/PH-formatted throughout. The earlier version treated a
    lone comma as a decimal separator, so '1,000 g (12 pcs)' silently parsed
    as 1.0 g instead of 1000 g: a 1000x under-deduction on every ingredient
    of a recipe line written with a thousands separator. The regex requires
    comma groups to be exactly three digits, so it cannot be fooled by a
    genuine (if unlikely in this data) decimal comma like '1,5'.

    A range ('35-40 g') is flagged ambiguous rather than silently averaged: it
    is a real measurement decision the owner has to make, not a rounding
    convenience for the importer.
    """
    raw = (text or "").strip()
    if not raw:
        return ParsedQty(0.0, "", raw, ambiguous=True)

    cleaned = raw.replace("–", "-").replace("—", "-").replace("½", "0.5")
    ambiguous = bool(re.search(r"\d\s*-\s*\d", cleaned))

    m = _QTY_RE.match(cleaned)
    if not m:
        return ParsedQty(0.0, "", raw, ambiguous=True)

    # group(1) can only contain commas as properly-grouped thousands
    # separators (the regex enforces 3-digit groups), so it is always safe
    # to strip them outright rather than treat them as a decimal point.
    qty = float(m.group(1).replace(",", ""))
    unit_token = (m.group(2) or "").lower()
    unit = _UNIT_ALIASES.get(unit_token, "")
    if not unit:
        # e.g. "1pc" caught above, but "2 cloves" has no measurable unit
        ambiguous = True
    return ParsedQty(qty, unit, raw, ambiguous=ambiguous)


def to_base(qty: float, unit: str, base_unit: str) -> float:
    """Convert a parsed quantity into the ingredient's base unit.

    Raises rather than falling back, because a wrong conversion here is silent
    and permanent — it lands in the ledger and trains every model downstream.
    """
    if base_unit not in BASE_UNITS:
        raise UnitConversionError(f"unknown base unit {base_unit!r}")
    unit = _UNIT_ALIASES.get((unit or "").lower(), unit or "")
    scale = _SCALE.get((unit, base_unit))
    if scale is None:
        raise UnitConversionError(
            f"cannot convert {unit or '(no unit)'!r} to {base_unit!r} without an "
            f"ingredient-specific factor"
        )
    return qty * scale


def purchase_to_base(
    purchase_qty: float,
    pack_size: float | None,
    yield_factor: float = 1.0,
) -> float:
    """How much usable stock a purchase quantity represents, in base units."""
    if pack_size is None:
        raise UnitConversionError("pack size is not set for this ingredient")
    if pack_size <= 0:
        raise UnitConversionError("pack size must be positive")
    if yield_factor <= 0:
        raise UnitConversionError("yield factor must be positive")
    return purchase_qty * pack_size * yield_factor


def base_to_purchase(base_qty: float, pack_size: float | None, yield_factor: float = 1.0) -> float:
    """Inverse of purchase_to_base, for sizing a reorder in whole packs."""
    if pack_size is None:
        raise UnitConversionError("pack size is not set for this ingredient")
    return base_qty / (pack_size * yield_factor)


def recipe_line_to_base(
    qty: float,
    unit: str,
    base_unit: str,
    pack_size: float | None = None,
    yield_factor: float = 1.0,
) -> float:
    """Convert a recipe line's quantity into an ingredient's base units.

    Tries the ordinary same-family conversion first (kg -> g, l -> ml, and so
    on). A recipe line written in *pieces* against an ingredient whose base
    unit is g/ml — "Eggs, 2 pcs (100 g)" against White Eggs, base_unit='g' —
    fails that path on purpose: to_base() has no ingredient-specific
    knowledge, and a generic "how many grams is a piece" guess is exactly the
    kind of silent wrong-deduction this codebase refuses to make.

    But the target ingredient's own pack_size *is* that per-piece weight —
    it is quite literally what pack_size means for a piece-counted
    ingredient (PIECE_WEIGHTS in gapfill.py). So when the ordinary
    conversion fails specifically because the recipe unit is 'pc' and the
    ingredient carries a pack_size, purchase_to_base() is the correct,
    non-guessing conversion: it uses the ingredient's own recorded
    grams-per-piece, not an invented one. Any other failure (a genuine
    cross-family mismatch, or an ingredient with no pack_size at all) still
    raises, and the caller routes the line to the unmapped queue as before.
    """
    try:
        return to_base(qty, unit, base_unit)
    except UnitConversionError:
        if unit == "pc" and base_unit != "pc" and pack_size:
            return purchase_to_base(qty, pack_size, yield_factor)
        raise


_WEIGHT_VOLUME_WORDS = {
    "kg", "kg.", "kilogram", "kilograms", "g", "g.", "gram", "grams",
    "l", "l.", "liter", "liters", "litre", "litres", "ml", "ml.",
}


def format_base(
    qty: float, base_unit: str,
    pack_size: float | None = None, purchase_unit: str | None = None,
) -> str:
    """Human-readable base quantity — kg/L above 1000 so a stock figure never
    reads as '24500 g' on an operator's screen.

    When pack_size and purchase_unit are given, also states how many of the
    actual purchase unit that comes out to — '4.80 Packs (2.40 kg)', '102 pcs.
    (5.10 kg)' — because a manager thinks in sacks of rice or bottles of oil,
    not grams. Skipped when purchase_unit is itself just a weight/volume word
    (kg, L, ...): the kg/L rollup above already says that, so repeating it
    would just be the same number twice."""
    if base_unit == "g" and abs(qty) >= 1000:
        base_str = f"{qty / 1000:.2f} kg"
    elif base_unit == "ml" and abs(qty) >= 1000:
        base_str = f"{qty / 1000:.2f} L"
    elif base_unit == "pc":
        base_str = f"{qty:.0f} pc"
    else:
        base_str = f"{qty:.1f} {base_unit}"

    if pack_size and purchase_unit and purchase_unit.strip().lower() not in _WEIGHT_VOLUME_WORDS:
        packs = qty / pack_size
        # Only once there is at least one whole container. Below that, state
        # the real remaining amount: "110 ml" is something a cook can pour,
        # "0.44 packs" is not a thing that exists on a shelf. The inventory
        # spec calls this exact case out by name for All Purpose Cream.
        if packs >= 1:
            return f"{packs:.2f} {purchase_unit} ({base_str})"
    return base_str
