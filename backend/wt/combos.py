"""Publishing an approved combo idea to the live menu/POS — the half of the
combo workflow migration 0036 deliberately left out ("publishing a combo to
the live menu is a distinct action outside this migration's and this
feature's scope").

A published combo is an ordinary menu_item row. It gets its own recipe, built
once at publish/edit time by COPYING each component's existing recipe_line
rows (scaled by how many of that component the combo contains) into the new
recipe — never by touching the component's own recipe, and never by nesting
a new sub_recipe. Copying at the recipe_line level rather than the fully
expanded leaf level is deliberate: a recipe_line whose display_label marks it
as a customer-choice flavor slot (see resolver.FLAVOUR placeholder
convention, matched by "%flavor%") must survive into the combo's recipe
exactly as-is, pointing at the same placeholder sub-recipe ingredient, or
wt/resolver.py's flavour_substitutions() — which looks up "%flavor%" lines on
the ORDER LINE'S OWN menu_item.recipe_id — would have nothing to find, and a
combo containing e.g. a Wings Platter would silently lose flavor selection
even though the plain Wings Platter still has it. Flattening through
resolver.expand_recipe() (as the combo cost preview does) would discard that
structure, so this module stops one level higher, at recipe_line.

Two different components that both carry a genuine flavor slot must never be
merged into one slot (that would destroy one of two independent customer
choices), so every flavor-slot line's display_label is prefixed with its
component's name before the merge key is computed — flavor slots from
different components can never collide. Two components sharing a plain
ingredient (both use the same raw "Rice") ARE merged into one combined
recipe_line, which is what the spec calls for and what keeps a kitchen ticket
from listing the same ingredient twice.

Unlimited items have no recipe at all — their consumption is a reconciled
lambda vector (wt/unli.py), not a BOM — so they are refused as combo
components outright; nothing here invents a recipe for one.
"""
from __future__ import annotations

import base64
import binascii
import io
import sqlite3
import uuid
from pathlib import Path

from . import audit
from .db import write_tx
from .errors import NotFoundError, ValidationError
from .resolver import expand_recipe
from .combo_pricing import menu_item_cost_breakdown, get_combo_pricing_config, suggest_price_range

# ---------------------------------------------------------------------------
# Image upload — base64 JSON body (not multipart: every other endpoint in
# this API is plain JSON, and the restaurant's own frontend already has to
# read the file as a data URL for the <img> preview, so sending that same
# base64 payload to the server avoids a second, differently-shaped upload
# path for one feature). Pillow verifies the bytes really decode as an image
# of a declared type before anything touches disk; a renamed .exe never gets
# an image extension just because the client's Content-Type said so.
# ---------------------------------------------------------------------------
try:
    from PIL import Image  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - Pillow is a declared dependency
    _PIL_AVAILABLE = False

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "uploads" / "menu_images"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def save_menu_item_image(content_base64: str) -> str:
    """Decodes, validates and saves an uploaded image; returns the
    image_path to store on menu_item (a URL path under /uploads/, never a
    filesystem path and never the client's own filename — the saved name is
    a fresh random one so nothing the client sends can choose where on disk
    this lands)."""
    if not _PIL_AVAILABLE:
        raise ValidationError("Image upload is not available on this server (Pillow not installed).")
    if "," in content_base64 and content_base64.strip().startswith("data:"):
        content_base64 = content_base64.split(",", 1)[1]
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("Image data is not valid base64.")
    if not raw:
        raise ValidationError("Image data is empty.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValidationError(f"Image is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        # verify() leaves the file object unusable for a second decode pass —
        # re-open before reading .format, the same two-step dance Pillow's
        # own docs describe for "verify then actually use it".
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
    except Exception:
        raise ValidationError("File is not a valid image.")

    ext = _ALLOWED_FORMATS.get(fmt or "")
    if ext is None:
        raise ValidationError(f"Unsupported image type '{fmt}'. Use JPEG, PNG, or WEBP.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    # Re-encode through Pillow rather than writing `raw` byte-for-byte: this
    # strips anything appended after a valid image payload (a classic
    # polyglot-file trick) and guarantees the bytes on disk are exactly what
    # Image.open() above actually decoded.
    img.convert("RGB" if ext != ".png" else img.mode).save(UPLOAD_DIR / filename)
    return f"/uploads/menu_images/{filename}"


def delete_menu_item_image(image_path: str | None) -> None:
    if not image_path:
        return
    p = UPLOAD_DIR / Path(image_path).name
    if p.is_file():
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Component validation — shared by preview, publish, and edit.
# ---------------------------------------------------------------------------
def _load_components(conn: sqlite3.Connection, components: list[dict]) -> list[dict]:
    """components: [{"menu_item_id": int, "quantity": float}, ...]. Returns
    the same list with each entry enriched with the component's row. Raises
    ValidationError (never a silent fallback) on anything that would make a
    correct combo recipe impossible to build."""
    if len(components) < 2:
        raise ValidationError("A combo needs at least 2 items.")

    seen_ids = set()
    out = []
    for comp in components:
        mid = comp["menu_item_id"]
        qty = comp["quantity"]
        if qty <= 0:
            raise ValidationError("Each component's quantity must be greater than 0.")
        if mid in seen_ids:
            raise ValidationError("The same item was added twice — combine it into one line with a higher quantity instead.")
        seen_ids.add(mid)

        row = conn.execute(
            "SELECT id, name, category, price, recipe_id, is_unli, is_combo FROM menu_item WHERE id=?",
            (mid,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"menu item {mid} not found")
        if row["is_unli"]:
            raise ValidationError(
                "Unlimited Wings cannot currently be included in a combo because its "
                "ingredient consumption is managed through a separate serving and "
                "reconciliation process."
            )
        if row["recipe_id"] is None:
            raise ValidationError(f"'{row['name']}' has no recipe yet, so it cannot be used in a combo.")
        out.append({"menu_item_id": mid, "quantity": qty, "row": row})
    return out


def preview_combo(conn: sqlite3.Connection, components: list[dict]) -> dict:
    """Read-only cost estimate for an arbitrary (2+) component list — the
    N-item generalisation of combo_pricing.estimate_combo_cost, reusing its
    per-item cost breakdown and the same labor/overhead settings. Never
    writes anything."""
    loaded = _load_components(conn, components)
    cfg = get_combo_pricing_config(conn)

    lines = []
    ingredient_cost = 0.0
    packaging_cost = 0.0
    unpriced: list[str] = []
    any_unverified = False
    any_incomplete = False
    current_menu_total = 0.0

    for comp in loaded:
        breakdown = menu_item_cost_breakdown(conn, comp["menu_item_id"])
        qty = comp["quantity"]
        lines.append({**breakdown, "quantity": qty})
        current_menu_total += (breakdown["price"] or 0.0) * qty
        if breakdown["cost_confidence"] in ("not_recipe_costed", "incomplete"):
            any_incomplete = True
            unpriced.extend(breakdown["unpriced_ingredients"])
            continue
        if breakdown["cost_confidence"] == "estimated":
            any_unverified = True
        ingredient_cost += (breakdown["ingredient_cost"] or 0.0) * qty
        packaging_cost += (breakdown["packaging_cost"] or 0.0) * qty

    if any_incomplete:
        ingredient_cost_out, packaging_cost_out, confidence = None, None, "incomplete"
    else:
        ingredient_cost_out = round(ingredient_cost, 2)
        packaging_cost_out = round(packaging_cost, 2)
        confidence = "estimated" if any_unverified else "confirmed"

    if cfg["labor_method"] is None:
        labor_cost, labor_status = None, "not_configured"
    elif cfg["labor_method"] == "fixed_per_serving":
        labor_cost, labor_status = (
            (round(cfg["labor_fixed_amount"], 2), "estimated")
            if cfg["labor_fixed_amount"] is not None else (None, "not_configured")
        )
    else:
        if cfg["labor_pct"] is None:
            labor_cost, labor_status = None, "not_configured"
        elif ingredient_cost_out is None:
            labor_cost, labor_status = None, "incomplete"
        else:
            labor_cost, labor_status = round(ingredient_cost_out * cfg["labor_pct"] / 100, 2), "estimated"

    if cfg["overhead_pct"] is None:
        overhead_cost, overhead_status = None, "not_configured"
    elif ingredient_cost_out is None or packaging_cost_out is None:
        overhead_cost, overhead_status = None, "incomplete"
    else:
        overhead_cost = round((ingredient_cost_out + packaging_cost_out) * cfg["overhead_pct"] / 100, 2)
        overhead_status = "estimated"

    components_sum = [ingredient_cost_out, packaging_cost_out, labor_cost, overhead_cost]
    if ingredient_cost_out is None:
        total_cost, total_status = None, "incomplete"
    else:
        total_cost = round(sum(v for v in components_sum if v is not None), 2)
        total_status = "complete" if all(v is not None for v in components_sum) else "partial"

    cost_estimate = {
        "items": lines,
        "ingredient_cost": ingredient_cost_out, "ingredient_status": confidence,
        "packaging_cost": packaging_cost_out,
        "labor_cost": labor_cost, "labor_status": labor_status,
        "overhead_cost": overhead_cost, "overhead_status": overhead_status,
        "total_cost": total_cost, "total_status": total_status,
        "unpriced_ingredients": sorted(set(unpriced)),
        "current_menu_total": round(current_menu_total, 2),
        "config": cfg,
    }

    price = suggest_price_range(conn, cost_estimate)
    return {"cost_estimate": cost_estimate, "price_suggestion": price}


# ---------------------------------------------------------------------------
# Recipe building — the merge described at the top of this file.
# ---------------------------------------------------------------------------
def _is_flavor_slot(display_label: str) -> bool:
    return "flavor" in display_label.lower() or "flavour" in display_label.lower()


def _merged_recipe_lines(conn: sqlite3.Connection, loaded_components: list[dict]) -> list[dict]:
    merged: dict[tuple[int, str], dict] = {}
    order = 0
    for comp in loaded_components:
        recipe_id = comp["row"]["recipe_id"]
        name = comp["row"]["name"]
        qty = comp["quantity"]
        source_lines = conn.execute(
            "SELECT ingredient_id, display_label, qty_base, is_optional "
            "FROM recipe_line WHERE recipe_id=? ORDER BY sort_order, id",
            (recipe_id,),
        ).fetchall()
        if not source_lines:
            raise ValidationError(f"'{name}' has a recipe with no ingredient lines, so it cannot be used in a combo.")
        for ln in source_lines:
            scaled_qty = ln["qty_base"] * qty
            if _is_flavor_slot(ln["display_label"]):
                key = (ln["ingredient_id"], f"{name} — {ln['display_label']}")
            else:
                key = (ln["ingredient_id"], ln["display_label"])
            if key in merged:
                merged[key]["qty_base"] += scaled_qty
            else:
                order += 1
                merged[key] = {
                    "ingredient_id": key[0],
                    "display_label": key[1],
                    "qty_base": scaled_qty,
                    "is_optional": bool(ln["is_optional"]),
                    "sort_order": order,
                }
    return list(merged.values())


def _write_combo_recipe(conn: sqlite3.Connection, recipe_pk: int, lines: list[dict]) -> None:
    conn.execute("DELETE FROM recipe_line WHERE recipe_id=?", (recipe_pk,))
    for ln in lines:
        conn.execute(
            "INSERT INTO recipe_line (recipe_id, ingredient_id, display_label, qty_base, "
            "qty_status, is_optional, sort_order) VALUES (?,?,?,?, 'verified', ?, ?)",
            (recipe_pk, ln["ingredient_id"], ln["display_label"], ln["qty_base"],
             1 if ln["is_optional"] else 0, ln["sort_order"]),
        )


def _next_code(conn: sqlite3.Connection, table: str, id_col: str, prefix: str) -> str:
    n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    while True:
        n += 1
        code = f"{prefix}-{n:04d}"
        if not conn.execute(f"SELECT 1 FROM {table} WHERE {id_col}=?", (code,)).fetchone():
            return code


# ---------------------------------------------------------------------------
# Publish / edit / list
# ---------------------------------------------------------------------------
def _combo_detail(conn: sqlite3.Connection, menu_item_pk: int) -> dict:
    row = conn.execute(
        "SELECT id, menu_item_id, name, category, price, recipe_id, image_path, "
        "pos_visible, is_combo, source_draft_combo_id, created_at, updated_at "
        "FROM menu_item WHERE id=? AND is_combo=1",
        (menu_item_pk,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"combo {menu_item_pk} not found")
    components = conn.execute(
        """
        SELECT cc.component_menu_item_id AS menu_item_id, cc.quantity, cc.sort_order,
               m.name, m.price, m.category
          FROM combo_component cc JOIN menu_item m ON m.id = cc.component_menu_item_id
         WHERE cc.combo_menu_item_id = ?
         ORDER BY cc.sort_order
        """,
        (menu_item_pk,),
    ).fetchall()
    return {
        "menu_item_id": row["id"],
        "code": row["menu_item_id"],
        "name": row["name"],
        "category": row["category"],
        "price": row["price"],
        "image_path": row["image_path"],
        "pos_visible": bool(row["pos_visible"]),
        "source_draft_combo_id": row["source_draft_combo_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "components": [dict(c) for c in components],
    }


def list_combos(conn: sqlite3.Connection) -> list[dict]:
    ids = conn.execute("SELECT id FROM menu_item WHERE is_combo=1 ORDER BY id DESC").fetchall()
    return [_combo_detail(conn, r["id"]) for r in ids]


def get_combo(conn: sqlite3.Connection, menu_item_pk: int) -> dict:
    return _combo_detail(conn, menu_item_pk)


def publish_combo(
    conn: sqlite3.Connection, *, name: str, category: str, price: float,
    components: list[dict], image_path: str | None = None,
    draft_combo_id: int | None = None, user_id: str | None = None,
) -> dict:
    name = name.strip()
    category = category.strip()
    if not name:
        raise ValidationError("Combo name is required.")
    if not category:
        raise ValidationError("Combo category is required.")
    if price < 0:
        raise ValidationError("Price cannot be negative.")

    loaded = _load_components(conn, components)
    lines = _merged_recipe_lines(conn, loaded)

    with write_tx(conn):
        recipe_code = _next_code(conn, "recipe", "recipe_id", "COMBO-REC")
        cur = conn.execute(
            "INSERT INTO recipe (recipe_id, name, kind, yield_qty, yield_unit) "
            "VALUES (?,?, 'menu', 1.0, 'serving')",
            (recipe_code, name),
        )
        recipe_pk = cur.lastrowid
        _write_combo_recipe(conn, recipe_pk, lines)

        menu_item_code = _next_code(conn, "menu_item", "menu_item_id", "COMBO")
        cur = conn.execute(
            "INSERT INTO menu_item (menu_item_id, name, category, price, recipe_id, "
            "is_unli, pos_visible, image_path, is_combo, source_draft_combo_id) "
            "VALUES (?,?,?,?,?, 0, 1, ?, 1, ?)",
            (menu_item_code, name, category, price, recipe_pk, image_path, draft_combo_id),
        )
        combo_pk = cur.lastrowid

        for idx, comp in enumerate(loaded):
            conn.execute(
                "INSERT INTO combo_component (combo_menu_item_id, component_menu_item_id, "
                "quantity, sort_order) VALUES (?,?,?,?)",
                (combo_pk, comp["menu_item_id"], comp["quantity"], idx),
            )
        audit.log(
            conn, "menu_item", combo_pk, "publish_combo", user_id,
            note=f"'{name}' at {price}, {len(loaded)} component(s)"
                + (f" from draft {draft_combo_id}" if draft_combo_id else ""),
        )

    return _combo_detail(conn, combo_pk)


def update_combo(
    conn: sqlite3.Connection, menu_item_pk: int, *,
    name: str | None = None, category: str | None = None, price: float | None = None,
    components: list[dict] | None = None, image_path: str | None = None,
    remove_image: bool = False, user_id: str | None = None,
) -> dict:
    existing = conn.execute(
        "SELECT id, recipe_id, image_path FROM menu_item WHERE id=? AND is_combo=1",
        (menu_item_pk,),
    ).fetchone()
    if existing is None:
        raise NotFoundError(f"combo {menu_item_pk} not found")

    fields, params = [], []
    if name is not None:
        if not name.strip():
            raise ValidationError("Combo name is required.")
        fields.append("name=?"); params.append(name.strip())
    if category is not None:
        if not category.strip():
            raise ValidationError("Combo category is required.")
        fields.append("category=?"); params.append(category.strip())
    if price is not None:
        if price < 0:
            raise ValidationError("Price cannot be negative.")
        fields.append("price=?"); params.append(price)

    old_image = existing["image_path"]
    new_image = old_image
    if remove_image:
        new_image = None
    elif image_path is not None:
        new_image = image_path
    if new_image != old_image:
        fields.append("image_path=?"); params.append(new_image)

    loaded = None
    if components is not None:
        loaded = _load_components(conn, components)
        lines = _merged_recipe_lines(conn, loaded)

    with write_tx(conn):
        if fields:
            fields.append("updated_at=datetime('now')")
            conn.execute(f"UPDATE menu_item SET {', '.join(fields)} WHERE id=?", (*params, menu_item_pk))
        if loaded is not None:
            _write_combo_recipe(conn, existing["recipe_id"], lines)
            conn.execute("DELETE FROM combo_component WHERE combo_menu_item_id=?", (menu_item_pk,))
            for idx, comp in enumerate(loaded):
                conn.execute(
                    "INSERT INTO combo_component (combo_menu_item_id, component_menu_item_id, "
                    "quantity, sort_order) VALUES (?,?,?,?)",
                    (menu_item_pk, comp["menu_item_id"], comp["quantity"], idx),
                )
        if fields or loaded is not None:
            audit.log(
                conn, "menu_item", menu_item_pk, "update_combo", user_id,
                note=", ".join(f.split("=")[0] for f in fields)
                    + (" +components" if loaded is not None else ""),
            )

    if new_image != old_image and old_image:
        delete_menu_item_image(old_image)

    return _combo_detail(conn, menu_item_pk)


def set_combo_visibility(conn: sqlite3.Connection, menu_item_pk: int, pos_visible: bool,
                         user_id: str | None = None) -> dict:
    """Pulling a combo off the POS (e.g. retiring it) without deleting the
    menu_item — same mechanism every other menu item already uses to go
    invisible (menu_item.pos_visible), never a delete, so sales history and
    past orders keep a valid menu_item to point at."""
    if not conn.execute("SELECT 1 FROM menu_item WHERE id=? AND is_combo=1", (menu_item_pk,)).fetchone():
        raise NotFoundError(f"combo {menu_item_pk} not found")
    with write_tx(conn):
        conn.execute(
            "UPDATE menu_item SET pos_visible=?, updated_at=datetime('now') WHERE id=?",
            (1 if pos_visible else 0, menu_item_pk),
        )
        audit.log(
            conn, "menu_item", menu_item_pk, "set_combo_visibility", user_id,
            field="pos_visible", new_value=pos_visible,
        )
    return _combo_detail(conn, menu_item_pk)
