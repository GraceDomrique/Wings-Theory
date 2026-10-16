"""Unlimited sessions and refill events.

    immutable paid order
      -> unli_session          the continuing context, one per unlimited order
        -> unli_refill         one append-only row per round served
          -> unli_refill_line  exactly what that round held and then deducted
        -> order_charge        a premium-flavour add-on, never folded into
                               customer_order.total

Why the order is not simply edited
----------------------------------
An unlimited ticket is paid once and served many times. The obvious
implementation — append each round to the order and re-total it — makes a
settled receipt a moving target, and there is no version of that which
survives a customer asking why their copy says something different from the
till's. So the paid order is frozen (migration 0011 makes its five money
columns physically unwritable) and everything that happens afterwards hangs
off it. "What the table owes now" is order.total + SUM(order_charge.amount),
computed when read.

What a refill deducts, and what it deliberately does not
--------------------------------------------------------
A refill consumes the SELECTED FLAVOUR'S SAUCE, and nothing else.

It does not deduct wings. The wings for the whole session already left the
ledger at commit, sized H x lambda by resolver.order_requirements — that is
the provisional deduction the reconciliation pipeline in wt/unli.py exists to
true up against a physical weighing. Deducting wings again per round would be
precisely the double deduction that module was written to make impossible,
and it would corrupt every variance figure downstream of it.

Sauce is the opposite case: it was never deducted for an unlimited order at
all. An unli order line has no recipe, and the four lambda streams are wings,
rice, fries and drinks. Fourteen flavour sub-recipes sat in the database with
nothing consuming them outside the a-la-carte platters, so every unlimited
round served since the system went live took sauce out of the walk-in that the
inventory never knew about. Recording the flavour is what makes deducting it
possible.

No second deduction engine
--------------------------
The quantities come from expand_recipe() — the same recursive BOM resolver the
POS gate and the sale path use. The hold is the same guarded UPDATE as
reservation._try_hold, the ledger write is reservation._ledger, and the batch
movement is reservation.deplete_fefo. This module supplies the *policy* (which
recipe, how much, whose round); every mechanism is the existing one.

Two phases, for the same reason orders have two
-----------------------------------------------
Submitting a round holds stock and puts it on the kitchen display; serving it
commits. That is not ceremony — it is what makes "wrong flavour, caught before
it was cooked" a release with no ledger impact, and "wrong flavour, caught
after" a compensating pair, exactly as release_order and void_after_commit
already distinguish them for orders.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3

from .db import write_tx
from .errors import InsufficientStockError, InvalidStateError, NotFoundError, ValidationError
from .reservation import EPS, _EXPIRED_BATCH, _ledger, _try_hold, deplete_fefo, restore_fefo
from .resolver import expand_recipe
from .unli import get_setting

DEFAULT_PIECES_PER_HEAD = 3
# Grams of finished sauce per head per round. The reference document sizes a
# 3-piece portion at 35-40 g (Sulit Deal 99), which is the same portion an
# unlimited head gets in one round, so a round is sized per HEAD rather than
# per piece: one head, three wings, 40 g of sauce. This replaced an earlier
# per-piece figure derived from the Wing Rack BOM — sizing by the piece implied
# a precision the source data does not support, and the document states the
# portion directly.
DEFAULT_SAUCE_G_PER_HEAD = 40.0

# Mirrors api.DEFAULT_TABLE_COUNT — kept as a separate constant rather than
# imported to avoid a refills<->api circular import; both read the same
# 'pos_table_count' app_setting, so an admin's configured count is honoured
# either way and this is only the bootstrap fallback before one is set.
DEFAULT_TABLE_COUNT = 9
_TABLE_LABEL_RE = re.compile(r"^Table (\d+)$")

# A round may only be recorded against a live session.
REFILL_OPEN_STATES = ("pending",)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def pieces_per_head(conn: sqlite3.Connection) -> int:
    return int(get_setting(conn, "unli_pieces_per_head", DEFAULT_PIECES_PER_HEAD))


def sauce_per_head(conn: sqlite3.Connection) -> float:
    return get_setting(conn, "unli_sauce_g_per_head", DEFAULT_SAUCE_G_PER_HEAD)


# ---------------------------------------------------------------------------
# flavours
# ---------------------------------------------------------------------------
def sync_flavors(conn: sqlite3.Connection) -> int:
    """Register any customer-selectable sauce that has no unli_flavor row yet.

    Idempotent, and safe to call at any time. Needed because migrations run
    against an empty database on a fresh install — the ingredients the 0011
    backfill selects from do not exist until seed.py has run — and useful
    afterwards, because a sauce added later should become selectable without
    a migration.

    'Customer-selectable' uses the rule /pos/flavors has always used: a
    sub-recipe ingredient whose name does not carry the '(house)' suffix.
    Cheese Sauce (house), Tartar Sauce (house) and Simple Syrup (house) are
    prep for other dishes, not something a customer picks for wings.

    Everything registers as 'included' at zero. No premium price exists
    anywhere in this database, and putting an invented one in front of a
    customer would be worse than offering none.
    """
    with write_tx(conn):
        cur = conn.execute(
            """
            INSERT INTO unli_flavor (ingredient_id, recipe_id, display_name, sort_order)
            SELECT i.id,
                   (SELECT r.id FROM recipe r
                     WHERE r.produces_ingredient_id = i.id AND r.is_active = 1),
                   i.name,
                   0
              FROM ingredient i
             WHERE i.kind = 'sub_recipe'
               AND i.is_active = 1
               AND i.name NOT LIKE '%(house)%'
               AND NOT EXISTS (SELECT 1 FROM unli_flavor f WHERE f.ingredient_id = i.id)
            """
        )
        added = cur.rowcount
        # Keep the display order stable and alphabetical without making the
        # caller supply one.
        conn.execute(
            """
            UPDATE unli_flavor SET sort_order = (
                SELECT COUNT(*) FROM unli_flavor f2
                 WHERE f2.display_name <= unli_flavor.display_name)
            """
        )
    return added


def list_flavors(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM v_unli_flavor"
    if active_only:
        sql += " WHERE is_active = 1 AND is_deductable = 1"
    return _rows(conn.execute(sql + " ORDER BY flavor_type, sort_order, display_name"))


def set_flavor(conn: sqlite3.Connection, flavor_pk: int, flavor_type: str | None = None,
               addon_price: float | None = None, addon_menu_item_id: int | None = None,
               is_active: bool | None = None, user_id: str | None = None) -> dict:
    """Configure one flavour. The only path that makes a flavour premium.

    An 'included' flavour is forced back to zero here rather than being
    allowed to keep a stale price: the CHECK constraint in 0011 would reject
    the row anyway, and failing with a constraint error would be a worse
    message than simply doing the obviously-correct thing.
    """
    with write_tx(conn):
        row = conn.execute("SELECT * FROM unli_flavor WHERE id=?", (flavor_pk,)).fetchone()
        if not row:
            raise NotFoundError(f"flavour {flavor_pk} not found")
        new_type = flavor_type or row["flavor_type"]
        if new_type == "included":
            price, menu_pk = 0.0, None
        else:
            price = row["addon_price"] if addon_price is None else addon_price
            menu_pk = row["addon_menu_item_id"] if addon_menu_item_id is None else addon_menu_item_id
            if price < 0:
                raise InvalidStateError("an add-on price cannot be negative")
        conn.execute(
            "UPDATE unli_flavor SET flavor_type=?, addon_price=?, addon_menu_item_id=?, "
            "is_active=?, updated_by=?, updated_at=datetime('now') WHERE id=?",
            (new_type, price, menu_pk,
             int(row["is_active"] if is_active is None else is_active), user_id, flavor_pk),
        )
    return dict(conn.execute("SELECT * FROM v_unli_flavor WHERE id=?", (flavor_pk,)).fetchone())


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def _order_is_unli(conn: sqlite3.Connection, order_pk: int) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id "
        "WHERE ol.order_id = ? AND m.is_unli = 1", (order_pk,)
    ).fetchone())


def open_session(conn: sqlite3.Connection, order_pk: int, user_id: str | None = None,
                 _in_tx: bool = False) -> dict:
    """Open the session for an unlimited order.

    Called from create_order inside its own transaction (_in_tx=True) so the
    order and its session are one atomic thing — an unlimited order that
    exists without somewhere to record its rounds is a ticket the floor cannot
    serve. Also callable on its own for an order that predates this feature.

    order_id is UNIQUE, so a retry cannot open a second session; the existing
    one is returned instead.
    """
    def _work():
        order = conn.execute(
            "SELECT id, order_no, head_count, status FROM customer_order WHERE id=?", (order_pk,)
        ).fetchone()
        if not order:
            raise NotFoundError(f"order {order_pk} not found")
        existing = conn.execute(
            "SELECT id FROM unli_session WHERE order_id=?", (order_pk,)).fetchone()
        if existing:
            return existing["id"]
        if not _order_is_unli(conn, order_pk):
            raise InvalidStateError(
                f"{order['order_no']} has no unlimited line — there is nothing to refill")
        if not order["head_count"]:
            raise InvalidStateError(
                "an unlimited session needs a head count — pieces per round are sized from it")
        cur = conn.execute(
            "INSERT INTO unli_session (order_id, head_count, created_by) VALUES (?,?,?)",
            (order_pk, order["head_count"], user_id),
        )
        return cur.lastrowid

    if _in_tx:
        session_pk = _work()
    else:
        with write_tx(conn):
            session_pk = _work()
    return session_detail(conn, session_pk)


def session_for_order(conn: sqlite3.Connection, order_pk: int) -> dict | None:
    row = conn.execute("SELECT id FROM unli_session WHERE order_id=?", (order_pk,)).fetchone()
    return session_detail(conn, row["id"]) if row else None


def session_detail(conn: sqlite3.Connection, session_pk: int) -> dict:
    row = conn.execute(
        "SELECT * FROM v_unli_session_summary WHERE session_id=?", (session_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"unlimited session {session_pk} not found")
    data = dict(row)
    data["pieces_per_head"] = pieces_per_head(conn)
    data["pieces_next_refill"] = data["head_count"] * data["pieces_per_head"]
    data["next_refill_no"] = (conn.execute(
        "SELECT COALESCE(MAX(refill_no), 0) AS n FROM unli_refill WHERE session_id=?",
        (session_pk,)).fetchone()["n"]) + 1
    data["refills"] = _rows(conn.execute(
        """
        SELECT r.*, f.display_name AS flavor_current_name
          FROM unli_refill r LEFT JOIN unli_flavor f ON f.id = r.flavor_id
         WHERE r.session_id = ? ORDER BY r.refill_no, r.id
        """, (session_pk,)))
    data["charges"] = _rows(conn.execute(
        "SELECT * FROM order_charge WHERE order_id=? ORDER BY id", (data["order_id"],)))
    data["accepts_refills"] = data["status"] == "active"
    data["head_flavors"] = json.loads(data["head_flavors"]) if data["head_flavors"] else []
    return data


def set_head_flavors(conn: sqlite3.Connection, session_pk: int, flavors: list[str | None],
                     user_id: str | None = None) -> dict:
    """Record which flavour each head at this unlimited table initially
    chose — repeats allowed (three diners can all pick Buffalo), capped at
    the session's own head_count, exactly as many "slots" as there are
    diners to fill.

    `flavors` is positional: index 0 is head 1, index 1 is head 2, and so
    on, with `None` meaning "not chosen yet." It is stored (and returned)
    that way rather than as a compacted list of only the chosen names, so
    that clearing head 2's flavour leaves head 3's exactly where it was —
    the till renders one fixed row per head, not a list that reflows when
    one entry in the middle is removed.

    Purely informational: this never touches a refill, a reservation, or the
    stock ledger. record_refill still sizes every actual served round off
    the session's head_count alone (see _record_refill_tx's own comment,
    "a till cannot talk the kitchen into a bigger round") — completely
    unchanged by this. This exists only so the POS can show and enforce
    "which flavour is head N having" against the true head count, before or
    between refill rounds, the way the floor already thinks about the table.

    Replaces the whole list on each call (not append-only) — the cashier
    correcting "actually head 2 wanted Garlic Parmesan, not Buffalo" is a
    normal, harmless edit to this informational record, not a financial or
    inventory event the way a served refill is.
    """
    with write_tx(conn):
        session = conn.execute(
            "SELECT head_count, status FROM unli_session WHERE id=?", (session_pk,)
        ).fetchone()
        if not session:
            raise NotFoundError(f"unlimited session {session_pk} not found")
        if session["status"] != "active":
            raise InvalidStateError(f"this session is {session['status']} — nothing to assign")
        if len(flavors) > session["head_count"]:
            raise ValidationError(
                f"{len(flavors)} head slots exceeds this table's {session['head_count']} head(s)"
            )
        chosen = [f for f in flavors if f is not None]
        if len(chosen) > session["head_count"]:
            raise ValidationError(
                f"{len(chosen)} flavour selections exceeds this table's {session['head_count']} head(s)"
            )
        valid_names = {f["display_name"] for f in list_flavors(conn, active_only=True)}
        unknown = [f for f in chosen if f not in valid_names]
        if unknown:
            raise ValidationError(f"not a selectable flavour: {', '.join(unknown)}")
        conn.execute(
            "UPDATE unli_session SET head_flavors=? WHERE id=?",
            (json.dumps(flavors) if chosen else None, session_pk),
        )
    return session_detail(conn, session_pk)


def set_session_table(conn: sqlite3.Connection, session_pk: int, table_label: str,
                      user_id: str | None = None) -> dict:
    """Assign the physical table for an already-open unlimited session.

    Deliberately not part of order creation: the cashier rings up the first
    serve (heads, flavours, payment) before the floor necessarily knows which
    table it's going to, and the till shouldn't block on that. The table is
    instead required here, from the refill screen, which is also where every
    later round is served against it — so 'Table 5' becomes the thing a
    cashier scans for on the refill list from the very first serve onward.

    Writes customer_order.table_label directly. Safe post-checkout: the
    immutability trigger from migration 0011 only guards the five money
    columns (subtotal/service_charge/total/cash_received/change_due) once
    total is set — table_label was never one of them, the same way
    status/served_at/voided_at are freely updated after checkout today.
    """
    with write_tx(conn):
        session = conn.execute(
            "SELECT order_id, status FROM unli_session WHERE id=?", (session_pk,)
        ).fetchone()
        if not session:
            raise NotFoundError(f"unlimited session {session_pk} not found")
        if session["status"] != "active":
            raise InvalidStateError(
                f"this session is {session['status']} — its table cannot be changed")
        m = _TABLE_LABEL_RE.match(table_label)
        if not m:
            raise ValidationError("choose a table from the grid")
        n = int(m.group(1))
        table_count = int(get_setting(conn, "pos_table_count", DEFAULT_TABLE_COUNT))
        if not (1 <= n <= table_count):
            raise ValidationError(f"choose a table between 1 and {table_count}")
        clash = conn.execute(
            """
            SELECT s.id FROM unli_session s JOIN customer_order o ON o.id = s.order_id
             WHERE s.status = 'active' AND o.table_label = ? AND s.id != ?
            """,
            (table_label, session_pk),
        ).fetchone()
        if clash:
            raise InvalidStateError(f"Table {n} is already in use by another active order")
        conn.execute(
            "UPDATE customer_order SET table_label=? WHERE id=?",
            (table_label, session["order_id"]),
        )
    return session_detail(conn, session_pk)


def list_sessions(conn: sqlite3.Connection, status: str | None = "active",
                  limit: int = 100) -> dict:
    sql = "SELECT * FROM v_unli_session_summary"
    params: list = []
    if status and status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    sessions = _rows(conn.execute(sql, params))
    for s in sessions:
        s["head_flavors"] = json.loads(s["head_flavors"]) if s["head_flavors"] else []
    return {"sessions": sessions}


def set_session_status(conn: sqlite3.Connection, session_pk: int, status: str,
                       note: str | None = None, user_id: str | None = None) -> dict:
    """Complete, cancel, or reopen a session.

    Completing releases any round still sitting unserved — a session cannot be
    closed with stock held against a refill nobody is going to cook. Reopening
    is a separate authorised action (see the API), not something a cashier
    reaches by accident, because the point of completing a session is that the
    table has left.
    """
    if status not in ("active", "completed", "cancelled"):
        raise InvalidStateError(f"unknown session status {status!r}")
    with write_tx(conn):
        row = conn.execute("SELECT * FROM unli_session WHERE id=?", (session_pk,)).fetchone()
        if not row:
            raise NotFoundError(f"unlimited session {session_pk} not found")
        if row["status"] == status:
            raise InvalidStateError(f"this session is already {status}")

        if status in ("completed", "cancelled"):
            for r in conn.execute(
                "SELECT id FROM unli_refill WHERE session_id=? AND status='pending'",
                (session_pk,),
            ).fetchall():
                _release_refill(conn, r["id"], f"session {status}", user_id)
            conn.execute(
                "UPDATE unli_session SET status=?, ended_at=datetime('now'), "
                "completed_by=?, note=COALESCE(?, note) WHERE id=?",
                (status, user_id, note, session_pk),
            )
        else:
            conn.execute(
                "UPDATE unli_session SET status='active', ended_at=NULL, "
                "reopened_by=?, reopened_at=datetime('now'), note=COALESCE(?, note) WHERE id=?",
                (user_id, note, session_pk),
            )
    return session_detail(conn, session_pk)


# ---------------------------------------------------------------------------
# refills
# ---------------------------------------------------------------------------
def _flavor_or_404(conn: sqlite3.Connection, flavor_pk: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM v_unli_flavor WHERE id=?", (flavor_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"flavour {flavor_pk} not found")
    if not row["is_active"]:
        raise InvalidStateError(f"{row['display_name']} is not currently offered")
    if not row["is_deductable"]:
        raise InvalidStateError(
            f"{row['display_name']} has no active recipe, so what it consumes cannot be "
            f"resolved — it cannot be served until that is fixed")
    return row


def _refill_requirements(conn: sqlite3.Connection, flavor: sqlite3.Row,
                         sauce_base: float) -> dict[int, float]:
    """Raw-ingredient requirement for one round, via the existing BOM resolver.

    The flavour's recipe yields `yield_qty` of sauce — 100 g, the master-ratio
    batch size from the reference document — so the multiplier is simply how
    many of those batches this round needs. Two heads at 40 g each is 80 g of
    sauce, or 0.8 of a batch, and every ingredient in the ratio scales by that
    one number. expand_recipe walks nested sub-recipes and stops at stocked
    ones exactly as it does for a menu item; no scaling logic is duplicated
    here.
    """
    recipe = conn.execute(
        "SELECT id, yield_qty FROM recipe WHERE id=?", (flavor["recipe_id"],)).fetchone()
    if not recipe or not recipe["yield_qty"]:
        raise InvalidStateError(f"{flavor['display_name']} has no usable recipe yield")
    multiplier = sauce_base / recipe["yield_qty"]
    need: dict[int, float] = {}
    for leaf in expand_recipe(conn, recipe["id"], multiplier):
        if not leaf.is_deductable:
            raise InsufficientStockError(
                f"{flavor['display_name']} cannot be deducted",
                shortfalls=[{"ingredient_id": leaf.ingredient_id, "name": leaf.name,
                             "reason": leaf.block_reason}],
            )
        if leaf.qty_base > 0:
            need[leaf.ingredient_id] = need.get(leaf.ingredient_id, 0.0) + leaf.qty_base
    return need


class _RefillRace(Exception):
    """Two submissions collided on the same round number or idempotency key.
    Raised inside the write transaction so it rolls back, then converted to a
    replay outside it."""


def _replay(conn: sqlite3.Connection, key: str | None) -> dict | None:
    if not key:
        return None
    row = conn.execute(
        "SELECT id FROM unli_refill WHERE client_request_id=?", (key,)).fetchone()
    if not row:
        return None
    out = refill_detail(conn, row["id"])
    out["duplicate"] = True
    return out


def _record_refill_tx(
    conn: sqlite3.Connection,
    session_pk: int,
    flavor_pk: int,
    user_id: str | None = None,
    client_request_id: str | None = None,
    note: str | None = None,
) -> int:
    """The body of a refill, assuming the caller holds the write transaction.

    Split out from record_refill so correct_refill can void one round and
    record its replacement inside a SINGLE transaction — write_tx refuses to
    nest (deliberately), and a correction that committed the void and then
    failed to record the replacement would leave the session a round short
    with no record of why.

    Pieces are 3 x head count (the multiplier is a setting, not a literal),
    read off the SESSION's head count rather than anything the client sends —
    a till cannot talk the kitchen into a bigger round.

    Stock is held, not yet deducted, exactly as punching an order holds it.
    """
    session = conn.execute("SELECT * FROM unli_session WHERE id=?", (session_pk,)).fetchone()
    if not session:
        raise NotFoundError(f"unlimited session {session_pk} not found")
    if session["status"] != "active":
        raise InvalidStateError(
            f"this session is {session['status']} — reopen it before serving another round")
    # Voiding an order releases or reverses its stock but says nothing about the
    # session hanging off it, so an active session outlived its own ticket: the
    # till kept listing the table, and a round served against it deducted sauce
    # and could attach a premium add-on to an order that had already been
    # cancelled. The session is closed at void (see api.void_order); this is the
    # check that makes the rule hold even for a session that predates that, or
    # one a manager reopened by hand.
    order = conn.execute(
        "SELECT order_no, status FROM customer_order WHERE id=?",
        (session["order_id"],)).fetchone()
    if order and order["status"] == "void":
        raise InvalidStateError(
            f"{order['order_no']} has been voided — it cannot be served another round")

    flavor = _flavor_or_404(conn, flavor_pk)
    heads = session["head_count"]
    pieces = heads * pieces_per_head(conn)
    # A round is still sized and cooked as ONE flavour for the whole table
    # (unchanged, immediately below) — but the heads it covers may each have
    # initially asked for something different (set_head_flavors). That
    # breakdown was being collected on the till and then never reaching the
    # kitchen at all: the SELECT FLAVOR grid's last tap decides flavor_id for
    # the round, so an earlier, different tap for another head was silently
    # dropped from what the kitchen sees. Snapshotting session["head_flavors"]
    # into this round's own `note` — an existing, already-displayed column,
    # not a new one — fixes exactly that, and has to happen here rather than
    # from the till: set_head_flavors resets to blank right after a round is
    # served (so the NEXT round starts clean), so by the time that PATCH
    # fires this round's assignments would already be gone if not captured
    # at creation time, inside the same transaction, before it.
    head_flavors = json.loads(session["head_flavors"]) if session["head_flavors"] else []
    if any(head_flavors):
        head_note = " · ".join(
            f"Head {i + 1}: {name}" for i, name in enumerate(head_flavors) if name
        )
        note = f"{note} — {head_note}" if note else head_note
    # Sauce is sized per head, not per piece — see DEFAULT_SAUCE_G_PER_HEAD.
    sauce_base = heads * sauce_per_head(conn)
    need = _refill_requirements(conn, flavor, sauce_base)
    if not need:
        raise InvalidStateError(f"{flavor['display_name']} resolved to no deductable ingredients")

    refill_no = conn.execute(
        "SELECT COALESCE(MAX(refill_no), 0) AS n FROM unli_refill WHERE session_id=?",
        (session_pk,),
    ).fetchone()["n"] + 1

    try:
        cur = conn.execute(
            """
            INSERT INTO unli_refill
              (session_id, refill_no, head_count, pieces, flavor_id, flavor_name,
               flavor_type, addon_amount, sauce_base, status, client_request_id,
               created_by, note)
            VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?,?,?)
            """,
            (session_pk, refill_no, heads, pieces, flavor["id"], flavor["display_name"],
             flavor["flavor_type"], flavor["addon_price"], sauce_base,
             client_request_id, user_id, note),
        )
    except sqlite3.IntegrityError as e:
        # Either two submits raced for the same round number, or the same
        # idempotency key arrived twice. Both mean somebody already has this
        # round; roll back and let the caller replay it.
        raise _RefillRace(str(e))

    refill_pk = cur.lastrowid

    # ascending ingredient_id — the same deterministic acquisition order
    # reserve_order uses, so the two cannot deadlock against each other once
    # this runs on a row-locking engine.
    shortfalls = []
    for ingredient_id in sorted(need):
        qty = need[ingredient_id]
        if _try_hold(conn, ingredient_id, qty):
            conn.execute(
                "INSERT INTO unli_refill_line (refill_id, ingredient_id, qty_base, state) "
                "VALUES (?,?,?, 'held')",
                (refill_pk, ingredient_id, qty),
            )
        else:
            # available mirrors exactly what _try_hold's own guard just tested
            # against -- see reservation.reserve_order's identical shortfall
            # query for why plain on_hand-reserved understates this whenever
            # expired stock is inflating on_hand_base.
            row = conn.execute(
                f"""
                SELECT name, base_unit, on_hand_base, reserved_base,
                       (SELECT COALESCE(SUM(qty_base), 0) FROM fefo_batch
                         WHERE ingredient_id = ingredient.id AND state = 'open'
                           AND ({_EXPIRED_BATCH})) AS expired_base
                  FROM ingredient WHERE id=?
                """,
                (ingredient_id,)).fetchone()
            shortfalls.append({
                "ingredient_id": ingredient_id, "name": row["name"],
                "needed": round(qty, 3),
                "available": round(row["on_hand_base"] - row["reserved_base"] - row["expired_base"], 3),
                "unit": row["base_unit"],
            })
    if shortfalls:
        # The caller's write_tx rolls back on this raise, undoing the refill
        # row and every hold taken above.
        raise InsufficientStockError(
            f"not enough stock to make {flavor['display_name']}", shortfalls=shortfalls)
    return refill_pk


def record_refill(conn: sqlite3.Connection, session_pk: int, flavor_pk: int,
                  user_id: str | None = None, client_request_id: str | None = None,
                  note: str | None = None) -> dict:
    """Serve a round. Append-only, idempotent, all-or-nothing.

    A retried or double-tapped submission carrying the same client_request_id
    replays the round that was already recorded rather than serving the table
    twice — the same mechanism, and the same reasoning, as /pos/orders.
    """
    replayed = _replay(conn, client_request_id)
    if replayed:
        return replayed
    try:
        with write_tx(conn):
            refill_pk = _record_refill_tx(
                conn, session_pk, flavor_pk, user_id, client_request_id, note)
    except _RefillRace:
        # Lost a genuine race: the winner committed microseconds earlier. That
        # round exists and the table has been served, so returning it is the
        # truthful answer — reporting a failure for a round that really went
        # out would have the floor serve it again.
        replayed = _replay(conn, client_request_id)
        if replayed:
            return replayed
        raise InvalidStateError("that round is already being recorded — refresh and check")
    return refill_detail(conn, refill_pk)


def serve_refill(conn: sqlite3.Connection, refill_pk: int, user_id: str | None = None) -> dict:
    """The kitchen has made it. Holds become real deductions, and a premium
    flavour's add-on is charged.

    The add-on is charged here rather than at submission because this is the
    point at which the food genuinely went to the table. A round cancelled
    before preparation should cost the customer nothing, and this is what
    makes that true without a compensating entry.
    """
    with write_tx(conn):
        refill = conn.execute("SELECT * FROM unli_refill WHERE id=?", (refill_pk,)).fetchone()
        if not refill:
            raise NotFoundError(f"refill {refill_pk} not found")
        if refill["status"] != "pending":
            raise InvalidStateError(
                f"refill #{refill['refill_no']} is already '{refill['status']}'")

        lines = conn.execute(
            "SELECT * FROM unli_refill_line WHERE refill_id=? AND state='held' "
            "ORDER BY ingredient_id", (refill_pk,)).fetchall()
        if not lines:
            raise InvalidStateError("this refill has no held stock to commit")

        session = conn.execute(
            "SELECT s.*, o.order_no FROM unli_session s JOIN customer_order o ON o.id = s.order_id "
            "WHERE s.id=?", (refill["session_id"],)).fetchone()

        for ln in lines:
            cur = conn.execute(
                """
                UPDATE ingredient
                   SET on_hand_base  = MAX(0, on_hand_base - :qty),
                       reserved_base = MAX(0, reserved_base - :qty),
                       updated_at    = datetime('now')
                 WHERE id = :id AND reserved_base >= :qty - :eps
                """,
                {"id": ln["ingredient_id"], "qty": ln["qty_base"], "eps": EPS},
            )
            if cur.rowcount != 1:
                raise InvalidStateError(
                    "this refill's hold no longer matches stock",
                    ingredient_id=ln["ingredient_id"])
            deplete_fefo(conn, ln["ingredient_id"], ln["qty_base"])
            _ledger(
                conn, ln["ingredient_id"], -ln["qty_base"], "sale_commit",
                "unli_refill", str(refill_pk), user_id,
                note=f"{session['order_no']} refill #{refill['refill_no']} — {refill['flavor_name']}",
            )
            conn.execute(
                "UPDATE unli_refill_line SET state='committed', resolved_at=datetime('now') "
                "WHERE id=?", (ln["id"],))

        conn.execute(
            "UPDATE unli_refill SET status='served', served_by=?, served_at=datetime('now') "
            "WHERE id=?", (user_id, refill_pk))

        if refill["flavor_type"] == "special" and refill["addon_amount"] > 0:
            conn.execute(
                """
                INSERT INTO order_charge
                  (order_id, refill_id, kind, amount, description, created_by)
                VALUES (?,?, 'unli_flavor_addon', ?, ?, ?)
                """,
                (session["order_id"], refill_pk, refill["addon_amount"],
                 f"Refill #{refill['refill_no']} — {refill['flavor_name']} (special flavor)",
                 user_id),
            )

    return refill_detail(conn, refill_pk)


def _release_refill(conn: sqlite3.Connection, refill_pk: int, reason: str,
                    user_id: str | None) -> None:
    """Drop the holds on a round that was never cooked. Writes nothing to the
    ledger, because no stock moved — the same reasoning as release_order, and
    the same reason it matters: a phantom movement here would land in the very
    dataset the forecast and anomaly models train on.

    Caller holds the transaction."""
    for ln in conn.execute(
        "SELECT * FROM unli_refill_line WHERE refill_id=? AND state='held'", (refill_pk,)
    ).fetchall():
        conn.execute(
            "UPDATE ingredient SET reserved_base = MAX(0, reserved_base - :qty), "
            "updated_at = datetime('now') WHERE id=:id",
            {"id": ln["ingredient_id"], "qty": ln["qty_base"]},
        )
        conn.execute(
            "UPDATE unli_refill_line SET state='released', resolved_at=datetime('now') WHERE id=?",
            (ln["id"],))
    conn.execute(
        "UPDATE unli_refill SET status='cancelled', voided_by=?, voided_at=datetime('now'), "
        "void_reason=? WHERE id=?", (user_id, reason, refill_pk))


def _void_refill_tx(conn: sqlite3.Connection, refill_pk: int, reason: str,
                    restock: bool, user_id: str | None) -> str:
    """Undo a round, assuming the caller holds the write transaction.

    Routes by state, exactly as voiding an order does:

      pending  nothing was cooked. Holds are released, no ledger entry, and
               no add-on was ever charged.

      served   the sauce was made. A `void_credit` reverses the deduction and
               a `wastage` entry immediately books it out again, so physical
               stock is unchanged but the trail says why — unless restock=True,
               which is for a serve fired in error where nothing was actually
               prepared. A premium add-on is reversed by a negative
               order_charge row pointing at the original; the original charge
               is never edited or removed, because order_charge is physically
               append-only.
    """
    refill = conn.execute("SELECT * FROM unli_refill WHERE id=?", (refill_pk,)).fetchone()
    if not refill:
        raise NotFoundError(f"refill {refill_pk} not found")
    if refill["status"] in ("cancelled", "voided"):
        raise InvalidStateError(f"refill #{refill['refill_no']} is already {refill['status']}")

    if refill["status"] == "pending":
        _release_refill(conn, refill_pk, reason, user_id)
        return "cancelled"

    for ln in conn.execute(
        "SELECT * FROM unli_refill_line WHERE refill_id=? AND state='committed' "
        "ORDER BY ingredient_id", (refill_pk,)
    ).fetchall():
        conn.execute(
            "UPDATE ingredient SET on_hand_base = on_hand_base + :qty, "
            "updated_at = datetime('now') WHERE id=:id",
            {"id": ln["ingredient_id"], "qty": ln["qty_base"]})
        restore_fefo(conn, ln["ingredient_id"], ln["qty_base"])
        _ledger(conn, ln["ingredient_id"], ln["qty_base"], "void_credit",
                "unli_refill", str(refill_pk), user_id, note=reason)
        if not restock:
            conn.execute(
                "UPDATE ingredient SET on_hand_base = MAX(0, on_hand_base - :qty), "
                "updated_at = datetime('now') WHERE id=:id",
                {"id": ln["ingredient_id"], "qty": ln["qty_base"]})
            deplete_fefo(conn, ln["ingredient_id"], ln["qty_base"])
            _ledger(conn, ln["ingredient_id"], -ln["qty_base"], "wastage",
                    "unli_refill", str(refill_pk), user_id, wastage_reason=reason)

    charge = conn.execute(
        "SELECT * FROM order_charge WHERE refill_id=? AND kind='unli_flavor_addon' "
        "AND NOT EXISTS (SELECT 1 FROM order_charge rv "
        "                 WHERE rv.reverses_charge_id = order_charge.id)",
        (refill_pk,)).fetchone()
    if charge:
        conn.execute(
            """
            INSERT INTO order_charge
              (order_id, refill_id, kind, amount, description, reverses_charge_id, created_by)
            VALUES (?,?, 'addon_reversal', ?, ?, ?, ?)
            """,
            (charge["order_id"], refill_pk, -charge["amount"],
             f"Reversal of refill #{refill['refill_no']} add-on — {reason}",
             charge["id"], user_id),
        )

    conn.execute(
        "UPDATE unli_refill SET status='voided', voided_by=?, voided_at=datetime('now'), "
        "void_reason=? WHERE id=?", (user_id, reason, refill_pk))
    return "voided"


def void_refill(conn: sqlite3.Connection, refill_pk: int, reason: str = "voided",
                restock: bool = False, user_id: str | None = None) -> dict:
    """Undo a round. Never a delete — the record stays and gains a reason."""
    with write_tx(conn):
        state = _void_refill_tx(conn, refill_pk, reason, restock, user_id)
    out = refill_detail(conn, refill_pk)
    out["state"] = state
    out["restocked"] = restock
    return out


def correct_refill(conn: sqlite3.Connection, refill_pk: int, flavor_pk: int,
                   reason: str = "wrong flavour", user_id: str | None = None,
                   client_request_id: str | None = None) -> dict:
    """Wrong flavour. Void the round and record a replacement that points back
    at it — both in ONE transaction, so a correction cannot half-happen.

    Deliberately two records rather than an edit: the kitchen really did make
    the first sauce (or really did not, if it was still pending), and the
    replacement is a genuinely new round of preparation. Editing the flavour
    on the original row would erase which of those happened, and with it the
    ingredients that were actually consumed.

    The replacement takes a NEW refill number rather than reusing the voided
    one. Round three going wrong does not make round four the third thing that
    came out of the kitchen.
    """
    replayed = _replay(conn, client_request_id)
    if replayed:
        return replayed

    try:
        with write_tx(conn):
            refill = conn.execute(
                "SELECT * FROM unli_refill WHERE id=?", (refill_pk,)).fetchone()
            if not refill:
                raise NotFoundError(f"refill {refill_pk} not found")
            if refill["status"] in ("cancelled", "voided"):
                raise InvalidStateError(
                    f"refill #{refill['refill_no']} is already {refill['status']}")
            was_pending = refill["status"] == "pending"
            original_no = refill["refill_no"]

            _void_refill_tx(conn, refill_pk, f"corrected: {reason}", False, user_id)
            replacement_pk = _record_refill_tx(
                conn, refill["session_id"], flavor_pk, user_id, client_request_id,
                note=f"replaces refill #{original_no}")
            conn.execute("UPDATE unli_refill SET corrects_refill_id=? WHERE id=?",
                         (refill_pk, replacement_pk))
    except _RefillRace:
        replayed = _replay(conn, client_request_id)
        if replayed:
            return replayed
        raise InvalidStateError("that correction is already being recorded — refresh and check")

    out = refill_detail(conn, replacement_pk)
    out["replaces_refill_id"] = refill_pk
    out["replaces_refill_no"] = original_no
    # A round caught before preparation cost nothing; one caught after left a
    # wastage entry behind, and the caller should be able to say so.
    out["original_was_prepared"] = not was_pending
    return out


def refill_detail(conn: sqlite3.Connection, refill_pk: int) -> dict:
    row = conn.execute(
        """
        SELECT r.*, s.order_id, o.order_no, s.status AS session_status
          FROM unli_refill r
          JOIN unli_session s ON s.id = r.session_id
          JOIN customer_order o ON o.id = s.order_id
         WHERE r.id = ?
        """, (refill_pk,)).fetchone()
    if not row:
        raise NotFoundError(f"refill {refill_pk} not found")
    data = dict(row)
    data["lines"] = _rows(conn.execute(
        """
        SELECT l.ingredient_id, i.name, l.qty_base, i.base_unit, l.state
          FROM unli_refill_line l JOIN ingredient i ON i.id = l.ingredient_id
         WHERE l.refill_id = ? ORDER BY i.name
        """, (refill_pk,)))
    return data


# ---------------------------------------------------------------------------
# receipt view — financial record plus its operational history
# ---------------------------------------------------------------------------
# Standard Philippine cash denominations a till actually has on hand.
_PH_CASH_DENOMINATIONS = (20, 50, 100, 200, 500, 1000)


def _reconstruct_cash_tender(total: float) -> tuple[float, float]:
    """A deterministic, realistic cash tender for a historical sale whose
    actual payment was never recorded in the source workbook (it has no
    payment-tender field at all — see wt/xlsx_history_import.py).

    Pure function of `total` alone: no randomness, no per-order seed, so the
    same order produces the exact same figures every time this runs, forever
    — the requirement is determinism across page loads, and a total is
    already a stable, immutable number once an order is settled, so nothing
    else is needed to guarantee that.

    Never persisted: migration 0011's trg_order_financials_immutable already
    (rightly) refuses to write cash_received/change_due back onto a row
    whose total is already set, and this does not try to get around that —
    it exists purely so a historical receipt can show a complete-looking
    payment section instead of "CASH — / CHANGE —", computed fresh on every
    read. The smallest standard bill that covers the total, so the
    reconstructed change is always a normal, unremarkable amount rather than
    an oddly large one.
    """
    if total <= 0:
        return 0.0, 0.0
    for note in _PH_CASH_DENOMINATIONS:
        if note >= total:
            return float(note), round(note - total, 2)
    tender = math.ceil(total / 500) * 500
    return float(tender), round(tender - total, 2)


def order_receipt(conn: sqlite3.Connection, order_pk: int) -> dict:
    """Everything a receipt shows: the settled sale exactly as it was paid,
    plus the unlimited session hanging off it if there is one.

    The two are kept visibly separate in the payload — `order` is the frozen
    financial record and `current_total` is derived arithmetic — so no client
    can mistake an add-on for a restatement of what was originally charged.
    """
    order = conn.execute(
        """
        SELECT id, order_no, status, table_label, head_count, created_by, created_at,
               committed_at, served_at, voided_at, void_reason, subtotal,
               discount_type, discount_amount, service_charge,
               total, cash_received, change_due, source, source_order_id,
               leftover_charge_total
          FROM customer_order WHERE id=?
        """, (order_pk,)).fetchone()
    if not order:
        raise NotFoundError(f"order {order_pk} not found")

    lines = _rows(conn.execute(
        """
        SELECT ol.qty, ol.unit_price, ol.note, m.name, m.is_unli
          FROM order_line ol JOIN menu_item m ON m.id = ol.menu_item_id
         WHERE ol.order_id = ? ORDER BY ol.id
        """, (order_pk,)))

    charges = _rows(conn.execute(
        "SELECT * FROM order_charge WHERE order_id=? ORDER BY id", (order_pk,)))
    addon_total = round(sum(ch["amount"] for ch in charges), 2)
    # 2026-10 fix: migration 0037's leftover_charge_total had no caller
    # anywhere, including here -- the receipt previously never showed a
    # leftover fee even once wt/reservation.set_leftover_charge() started
    # writing one. Additive, same as addon_total, never folded into the
    # frozen `total`/`order` dict itself.
    leftover_charge_total = round(order["leftover_charge_total"] or 0.0, 2)
    original_total = order["total"]

    # A historical sale genuinely has no recorded cash tender (the source
    # workbook has no payment field at all) — reconstructed_* are computed
    # fresh here, kept OUT of the `order` dict (which stays the untouched,
    # frozen database record — never silently rewritten to imply the source
    # actually recorded a payment), and flagged so a client can label them
    # honestly rather than presenting them as a source-recorded fact.
    payment_reconstructed = (
        order["source"] == "historical"
        and order["cash_received"] is None
        and original_total is not None
    )
    reconstructed_cash = reconstructed_change = None
    if payment_reconstructed:
        reconstructed_cash, reconstructed_change = _reconstruct_cash_tender(original_total)

    session = session_for_order(conn, order_pk)
    return {
        "order": dict(order),
        "lines": lines,
        "session": session,
        "charges": charges,
        "addon_total": addon_total,
        "leftover_charge_total": leftover_charge_total,
        "original_total": original_total,
        "payment_reconstructed": payment_reconstructed,
        "reconstructed_cash_received": reconstructed_cash,
        "reconstructed_change_due": reconstructed_change,
        "current_total": round((original_total or 0) + addon_total + leftover_charge_total, 2)
                         if original_total is not None else None,
    }
