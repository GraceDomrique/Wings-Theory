/**
 * Shared data contracts — spec section 3, transcribed as JSDoc typedefs so the
 * editor type-checks them the same way the .ts interfaces would have.
 *
 * These are the shapes the UI renders. They are NOT the shapes the backend
 * returns: the real API speaks snake_case and uses different names
 * (`days_remaining` for `daysLeft`, `avg_daily_consumption` for `usedPerDay`,
 * `available_base` for `onHandBaseQty`, and so on). Each file under lib/api/
 * owns the adapter from one real endpoint to one of these types, so the
 * mapping lives in exactly one place per domain and the routes never see a
 * raw backend row.
 *
 * Where a field simply does not exist in the backend yet, the adapter sets it
 * to null and the UI renders the "no data" state — it is never defaulted to 0.
 *
 * This module exports nothing at runtime by design.
 */

/** @typedef {'crit'|'warn'|'good'|'info'|'flat'} Severity */

/**
 * @typedef {Object} Ingredient
 * @property {string} id                  ING-0061 (the human id; `pk` is the FK)
 * @property {number} pk                  the integer primary key the API takes
 * @property {string} name
 * @property {'Wet food'|'Dry food'|'Consumable'|'Operational supply'|string} category
 * @property {string} onHandUnit          "packs", "bottles", "g"
 * @property {number|null} onHandBaseQty  normalized (grams / ml)
 * @property {string} baseUnit
 * @property {string|null} onHandDisplay  backend-rendered "2.00 bottles (1.00 L)"
 * @property {number|null} usedPerDay     null = not enough sales history
 * @property {number|null} daysLeft       null = insufficient data, never 0
 * @property {string|null} expiryDate
 * @property {'confirmed'|'unverified'} confidence
 * @property {number|null} costPerUnit    null = missing a cost, never render P0
 * @property {boolean} isDeductable
 * @property {string|null} blockReason
 * @property {number} unverifiedCount
 */

/**
 * @typedef {Object} StockBatch
 * @property {string} batchId             SEED-0070
 * @property {number} pk
 * @property {string} ingredientId
 * @property {string} ingredientName
 * @property {string|null} receivedDate
 * @property {string|null} expiryDate
 * @property {'exact'|'estimated'|'none'} expirySource
 * @property {number|null} qtyRemaining
 * @property {string} baseUnit
 * @property {number} drawOrder           1 = next to be consumed (FEFO)
 * @property {number|null} daysToExpiry
 * @property {boolean} expired
 */

/**
 * @typedef {Object} StockMovement
 * @property {string} id
 * @property {string} timestamp
 * @property {string} ingredientId
 * @property {string} ingredientName
 * @property {'sale_deduction'|'order_voided_return'|'receiving'|'wastage'|'stock_count'|'correction'|string} reason
 * @property {number|null} changeQty      signed
 * @property {number|null} stockAfter
 * @property {string} baseUnit
 * @property {string|null} linkedOrderId
 * @property {string|null} note
 */

/**
 * @typedef {Object} MenuItem
 * @property {string} id
 * @property {number} pk
 * @property {string} name
 * @property {string} category
 * @property {number|null} price
 * @property {boolean} sellable
 * @property {string|null} blockingIngredientId  why it is unavailable, if not sellable
 * @property {string|null} blockReason           backend-authored, never inferred here
 * @property {string|null} recipeId
 */

/**
 * @typedef {Object} RecipeLine
 * @property {string} ingredientId
 * @property {string} label
 * @property {number|null} amount
 * @property {string} unit
 * @property {number|null} cost
 */

/**
 * @typedef {Object} Recipe
 * @property {string} id
 * @property {number} pk
 * @property {string} name
 * @property {string|null} menuItemId
 * @property {RecipeLine[]} lines
 * @property {number|null} costPerServing
 * @property {number|null} marginPct
 * @property {number} lineCount
 * @property {number} unmappedCount
 */

/**
 * @typedef {Object} ForecastPoint
 * @property {string} date
 * @property {number} predictedUnits
 * @property {number|null} low
 * @property {number|null} high
 */

/**
 * @typedef {Object} ForecastSeries
 * @property {string} menuItemId
 * @property {string} name
 * @property {number} daysOfHistory      < 7 => insufficient data; show the gap
 *                                       message, never a fabricated flat line
 * @property {number} requiredDays
 * @property {boolean} insufficientData
 * @property {ForecastPoint[]} series    the forecast horizon
 * @property {{date:string, qty:number}[]} history  actual sales behind it
 * @property {string} method             'prophet' | 'trailing_average'
 * @property {{mae:number, rmse:number, mape:number|null}|null} accuracy
 */

/**
 * @typedef {Object} ReplenishmentRow
 * @property {string} ingredientId
 * @property {string} name
 * @property {number|null} onHand
 * @property {string} baseUnit
 * @property {number|null} usedPerDay
 * @property {number|null} daysLeft
 * @property {number|null} leadTimeDays
 * @property {number|null} reorderPoint       null = system-suggested, not manager-set
 * @property {number|null} suggestedReorderPoint
 * @property {number|null} suggestedOrderQty
 * @property {string|null} suggestedOrderUnit
 * @property {boolean} belowReorderPoint
 * @property {Severity} status
 * @property {string|null} supplier
 * @property {string|null} purchaseUnit
 * @property {number|null} packSize
 * @property {number|null} minimumOrderQty    supplier-imposed floor on a single reorder, in suggestedOrderUnit
 * @property {number} incomingOrderedBase     already on a confirmed PO, not yet received; already netted out of suggestedOrderQty
 */

/**
 * @typedef {Object} AnomalyFlag
 * @property {string} id
 * @property {string} ingredientId
 * @property {string} ingredientName
 * @property {string} timestamp
 * @property {number|null} amount
 * @property {string} baseUnit
 * @property {'wastage'|'spoilage'|'count_correction'|string} reasonCode
 * @property {string} flaggedBecause      human-readable, backend-generated.
 *                                        Factual only — see spec section 6
 *                                        point 5: never accusatory copy.
 * @property {'review_soon'|'worth_a_look'} priority
 * @property {Severity} severity
 */

/**
 * @typedef {Object} ReconciliationStream
 * @property {string} stream              'Unlimited Wings' | 'Unlimited Rice' | ...
 * @property {number} pk
 * @property {string} deductsFrom         ingredient name the stream draws down
 * @property {number|null} coefficient    lambda, current rolling
 * @property {number|null} baselineCoefficient  frozen
 * @property {number|null} driftPct
 * @property {number|null} windowDays
 * @property {string} baseUnit
 */

/**
 * @typedef {Object} VerificationHistoryEvent
 * @property {string|null} value
 * @property {string|null} changedBy
 * @property {string|null} timestamp
 */

/**
 * @typedef {Object} VerificationItem
 * @property {string} id                  field + row_id, the resolve() key
 * @property {string} field               'pack_size' | 'yield' | 'recipe_qty' | ...
 * @property {number} rowId
 * @property {string} entityName          the ingredient or recipe it belongs to
 * @property {1|2|3} priority
 * @property {string} priorityLabel
 * @property {string} groupLabel
 * @property {string} question            backend-authored, plain language
 * @property {string|null} currentValue
 * @property {string|null} whyItMatters
 * @property {string|null} whatToCheck
 */

/**
 * @typedef {Object} DashboardSummary
 * @property {Object} kpis
 * @property {Object[]} alerts
 * @property {Object} forecastPreview
 * @property {Object[]} useFirst        renamed from expiryPreview, 2026-09
 * @property {Object[]} verifyPreview
 */

export {};
