"""The curated alias map: recipe ingredient label -> stock item name.

This file is the human decision, made once and written down. It is the reason
the importer never fuzzy-matches: every mapping here was reviewed, and anything
not listed goes to the Unmapped Ingredients queue for an owner decision rather
than being guessed at.

Updated for the owner's corrected spreadsheets (2026-08-11 revision), which
resolved most of the earlier data gaps directly: 14 of the 18 ingredients that
previously had no stock record at all are now real inventory rows with real
quantities (see "Additional Ingredients (Recipe-Required)" in the inventory
sheet), so their entries move from DEFERRED to ALIASES below. Several
previously-ambiguous garnish/flavour labels were also reworded by the owner
into unambiguous ones ("Garnish (parsley/spring onion, optional)" became
"Parsley (default garnish)"), which is why the DEFERRED list is now much
shorter than before.

Four categories below, and the distinction matters:

  ALIASES         Same physical product, different label — resolves
                   immediately during import, no queue involved.

  SUB_RECIPE_ALIASES  Recipe lines that reference a sub-recipe (a sauce or
                   dip produced in-house) rather than a raw stock item.
                   Points at the ingredient name gapfill.py creates.

  FLAVOUR_PLACEHOLDER  The customer picks the flavour at the till, so the BOM
                   cannot name one. Maps to a house default sub-recipe; the
                   POS overrides per order once flavour selection exists.

  DEFERRED        Deliberately not mapped. Each carries the reason it went to
                   the queue. Guessing any of these would put a wrong
                   deduction into the ledger permanently.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Same product, different wording. Value = exact `name` in the stock list.
# ---------------------------------------------------------------------------
ALIASES: dict[str, str] = {
    # seasoning / pantry naming variants
    "black pepper": "Ground Pepper",
    "ground black pepper": "Ground Pepper",
    "salt": "Iodized Salt",
    "garlic powder": "Garlic powder",
    "sesame seed": "Sesame Seed",
    "sesame seeds": "Sesame Seed",
    "parsley": "Parsley",
    "chili flakes dehydrated": "Chili Flakes Dehydrated",
    "dehydrated chili flakes": "Chili Flakes Dehydrated",   # word order swapped in the 08-11 sheet
    "paprika": "Paprika",
    "refined sugar": "Refined Sugar",
    "cornstarch": "Cornstarch",
    "cornstarch slurry": "Cornstarch",
    "honey": "Honey",
    "sriracha": "Sriracha",
    "soy sauce": "Soy Sauce",
    "worchestershire": "Worchestershire",
    "worcestershire sauce": "Worchestershire",   # correct spelling in the recipe; the
                                                  # stock item itself is misspelled
    "garlic flakes": "Garlic Flakes",
    "kimchi": "Kimchi",
    "milk powder": "Milk Powder",
    "cheese powder": "Cheese Powder",
    "parmesan cheese": "Parmesan Cheese",
    "bread crumbs": "Bread Crumbs",
    "red tea powder": "Red Tea Powder",
    "ram pouch pickle relish": "Ram Pouch pickle relish",
    "mushroom pieces and stems": "Mushroom Pieces and stems",
    "shitake mushroom": "Shitake Mushroom",
    "evaporated milk": "Evaporated Milk",
    "buffalo hot sauce": "Buffalo Hot Sauce",

    # spelling differences carried over from the owner's own sheet
    "hungarian sausage": "Hugarian Sausage",
    "condensed milk": "Condense Milk",
    "mama sita s oyster sauce": "Mama Sitas Oyster Sauce Big",
    "gochujang": "Gochujang Big",

    # different word for the same stocked item
    "cooking oil": "Palm Oil",
    "cooking oil palm oil": "Palm Oil",   # "Cooking Oil (Palm Oil)" — new explicit label
    "dry pasta": "Pasta",
    "pasta": "Pasta",
    "linguine pasta": "Linguine Pasta",
    "balducci penne pasta": "Balducci Penne Pasta",
    "frozen french fries": "Fries",
    "fries": "Fries",
    "plastic straw": "Shake Straw",
    "ice cubes": "Tube Ice",
    "tissue": "Tissue Interfold",
    "parchment paper": "Parchment Paper",
    "parchment paper tray liner": "Parchment Paper",
    "garlic": "Fresh Garlic Peeled",
    "all purpose flour": "Flour",
    "frying flour breading": "Flour",
    "flour": "Flour",
    "butter": "Butter",
    "cooking oil butter": "Butter",     # "Cooking Oil / Butter (for eggs)" — old sheet only
    "all purpose cream": "All purpose Cream",
    "mayonaise": "Mayonaise",
    "mayonnaise": "Mayonaise",          # correct spelling in the recipe; stock item
                                         # keeps the owner's single-N spelling
    "japanese mayo": "Mayonaise",       # not stocked separately; close enough for
                                         # deduction purposes — flavour differs, cost doesn't
    "hot sauce": "Hot Sauce",
    "canned tuna": "Canned Tuna Big Can",
    "pesto sauce": "Pesto Jar",
    "pesto": "Pesto Jar",
    "fish fillet": "Fish Fillet",
    "spam": "Spam",
    "coke in can": "Coke in Can",
    "sprite in can": "Sprite in Can",
    "royal in can": "Royal in Can",

    # proteins where the label describes preparation, not a different product
    "marinated beef tapa": "Beef Tapa",
    "tocino": "Tocino",
    "bacon": "Bacon",
    "bacon cooked chopped": "Bacon",
    "chicken wings": "Wings",
    "raw chicken wings": "Wings",
    "chicken": "Chicken Breast/Thigh (raw, boneless)",       # Salpicao/Alfredo: cubed/sliced
    "pork chop": "Pork Chop (Taiwanese cut, boneless)",
    "pork loin": "Pork Chop (Taiwanese cut, boneless)",      # Tonkatsu is cut from loin;
                                                              # same purchased item

    # produce and eggs counted in pieces, consumed by weight
    "egg": "White Eggs",
    "eggs": "White Eggs",
    "white egg": "White Eggs",
    "salted eggs": "Salted Eggs",
    "lemon": "Lemon",
    "orange": "Orange",
    "mango": "Mango",
    "ripe mango flesh": "Mango",
    "banana": "Banana",

    # now real inventory rows ("Additional Ingredients (Recipe-Required)" in
    # the 08-11 sheet) with actual owner-counted quantities — these used to be
    # DEFERRED to the queue because no stock record existed at all.
    "truffle oil": "Truffle Oil",
    "olive oil": "Olive Oil",
    "fresh mushrooms": "Fresh Mushrooms",
    "fresh milk": "Fresh Milk",
    "cucumber": "Cucumber",
    "onion": "Onion",
    "blueberry puree": "Blueberry Puree",
    "plain yogurt": "Plain Yogurt",
    "shredded cabbage": "Shredded Cabbage",
    "pickled papaya": "Pickled Papaya (Atchara)",
    "tonkatsu sauce": "Tonkatsu Sauce",
    "spring onion": "Spring Onion",
    # Cooking Cream is the one "Additional Ingredient" the 08-11 sheet still
    # does not stock — every OTHER cream-based recipe in the same sheet
    # (Creamy Sausage Pasta, Creamy Chicken Alfredo) already switched to using
    # the stocked All Purpose Cream, so the same substitution is used here
    # rather than leaving White Truffle Pasta's one reference queued.
    "cooking cream": "All purpose Cream",

    # rice: recipes are written in COOKED grams, stock is bought raw.
    # The raw->cooked gain lives in Rice.yield_factor, so base units are
    # "grams of cooked rice" throughout. See seed.py YIELDS.
    "steamed rice": "Rice",
    "steamed garlic rice": "Rice",
    "rice": "Rice",
}

# ---------------------------------------------------------------------------
# Sub-recipes: produced in-house, resolved via a fixed composition built by
# gapfill.py (gapfill.SUB_RECIPES) rather than parsed from the BOM sheet —
# the 08-11 revision expresses these as percentage-of-batch formulas ("Cheese
# Sauce (Sub-Recipe Definition)") or per-kg-of-wings ratios ("Unlimited Wings
# - 14 Flavors"), not literal per-ingredient recipe rows, so raw_import.py
# skips those blocks and gapfill.py constructs the recipes programmatically.
#
# Key = normalise_label() of the recipe-line text that references the
# sub-recipe. Value = the exact ingredient name gapfill.py creates.
# ---------------------------------------------------------------------------
SUB_RECIPE_ALIASES: dict[str, str] = {
    # NOTE: normalise_label() strips a trailing "(sub-recipe)" entirely
    # (parenthetical content is discarded, not turned into words), so
    # "Cheese Sauce (sub-recipe)" normalises to "cheese sauce", not
    # "cheese sauce sub recipe" — verified against the actual BOM rows.
    "buffalo sauce": "Classic Buffalo Sauce",             # Wings Platter
    "korean bbq sauce": "Korean BBQ Sauce",                # Spicy Korean Wing Bowl
    "ranch dip": "Ranch Dip",                              # Ranch Dip add-on, Wing Rack
    "cheese sauce": "Cheese Sauce (house)",                # Cheesy Bacon Fries
    "tartar sauce": "Tartar Sauce (house)",                # Fish & Chips
    "simple syrup": "Simple Syrup (house)",                # the three shakes
}

# ---------------------------------------------------------------------------
# Flavour chosen by the customer at the till. Deducting a specific sauce from
# a recipe would be wrong, so these map to the house default and the POS
# overrides per order once flavour selection exists.
# ---------------------------------------------------------------------------
FLAVOUR_PLACEHOLDER: dict[str, str] = {
    "flavor sauce": "Classic Buffalo Sauce",
    "flavor sauce 1": "Classic Buffalo Sauce",   # "Flavor sauce #1" (Wing Rack, 08-11 sheet)
    "flavor sauce 2": "Classic Buffalo Sauce",   # "Flavor sauce #2"
}

# Default gram quantity for a FLAVOUR_PLACEHOLDER line whose source text
# carries no number at all — Wing Rack's is literally "per the selected
# flavor's ratio (see Unlimited Wings block)", pure prose, so parse_quantity
# finds nothing and returns qty=0. Silently deducting zero sauce on every
# Wing Rack sale is wrong, not merely imprecise, so a documented default is
# used instead of letting the line vanish.
#
# 60 g = half of Classic Buffalo's reconciled 120 g-per-kg-wings ratio
# (gapfill.SUB_RECIPES) applied to the 500 g half-rack each flavour covers —
# matches the previous BOM revision's explicit "Flavor Sauce A/B (half rack)
# 60 g" figure exactly. Sulit Deals' "Flavor Sauce (any flavor)" line is NOT
# listed here because its source text does carry a real number ("35-40 g")
# and resolves through the ordinary parsing path.
FLAVOUR_PLACEHOLDER_DEFAULT_QTY: dict[str, float] = {
    "flavor sauce 1": 60.0,
    "flavor sauce 2": 60.0,
}

# ---------------------------------------------------------------------------
# Deliberately unmapped. label -> reason code understood by the queue.
#   no_stock_record : the shop does not currently carry this at all
#   ambiguous       : could plausibly be two different stock items
#   utility         : not inventory in any meaningful sense
# ---------------------------------------------------------------------------
# NOTE: keys here are the output of units.normalise_label, which strips
# parentheticals. "Chicken (cubed)" and "Chicken (sliced)" both normalise to
# "chicken" — that collision is resolved (see ALIASES above), not deferred.
DEFERRED: dict[str, str] = {
    # panko is a distinct product from the stocked Bread Crumbs, and the
    # 08-11 sheet still doesn't carry it separately — stays a queued,
    # zero-stock ingredient (gapfill.ADDITIONS) rather than a substitution.
    "japanese breadcrumbs": "ambiguous",

    # consumed but never counted as inventory
    "water": "utility",
}
