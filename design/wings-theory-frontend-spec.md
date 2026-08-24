# Wings Theory Console — Frontend Rebuild Spec
For Claude Code. Reference: `wings-theory-console.html` (the approved visual/IA prototype). This doc translates that prototype into a React component tree, data contracts, and design tokens so the app is built once, correctly — not reverse-engineered from screenshots again.

---

## 1. Design tokens

Put these in `tailwind.config.js` (or a `tokens.css` if not using Tailwind). Do not introduce new colors outside this palette.

```js
colors: {
  bg:        '#F7F5F0',
  surface:   '#FFFFFF',
  sunk:      '#FBF9F4',
  line:      '#E7E2D8',
  lineSoft:  '#F0EDE4',
  ink:       '#1F1C17',
  inkSoft:   '#6E6656',
  inkFaint:  '#A39A87',
  amber:     '#C6791E',
  amberDeep: '#9C5814',
  amberTint: '#FBEBD3',
  good:      '#3E7A55',
  goodTint:  '#E4F1E7',
  warn:      '#B4791B',
  warnTint:  '#FBEED8',
  crit:      '#B23E30',
  critTint:  '#FBE6E2',
  info:      '#3D6A96',
  infoTint:  '#E5EEF6',
},
fontFamily: {
  display: ['Fraunces', 'serif'],   // headings, big numbers
  body:    ['Inter', 'sans-serif'], // everything else
  mono:    ['JetBrains Mono', 'monospace'], // codes, quantities, timestamps
},
borderRadius: { DEFAULT: '10px', sm: '6px' },
```

**Severity color rule (applies everywhere — table rows, cards, pills, chart bars):**
`crit` = expired / blocking / out of tolerance · `warn` = expiring soon / needs review · `good` = on track / confirmed · `info` = neutral/reference · `inkFaint` = no data yet.
Never invent a 5th status color. If something doesn't fit these four, it's "flat" (gray pill, no dot).

---

## 2. File structure

```
src/
  app/
    layout/
      AppShell.tsx          // grid: Sidebar + Main
      Sidebar.tsx            // nav groups, active state, badge counts
      Topbar.tsx              // page title/sub, search, notifications
    routes/
      today/Today.tsx
      inventory/
        Inventory.tsx         // tab shell
        OnHand.tsx
        Batches.tsx
        Receiving.tsx
        Movements.tsx
      menu/
        Menu.tsx              // tab shell
        MenuAvailability.tsx
        Recipes.tsx
      intelligence/
        Intelligence.tsx      // tab shell
        Forecasting.tsx
        Replenishment.tsx
        AnomalyDetection.tsx
        Reconciliation.tsx
      expiry/Expiry.tsx
      verify/Verification.tsx
      reports/Reports.tsx
      system/System.tsx
  components/
    ui/
      Card.tsx               // Card, CardHeader, CardTitle, CardDesc
      Pill.tsx                // status pill, variant = crit|warn|good|info|flat
      Stat.tsx                 // big number + label
      Meter.tsx                 // runway/expiry progress bar
      Tabbar.tsx                 // subtab switcher
      RailCard.tsx                 // card with colored left border
      Table.tsx                     // thin wrapper: sortable, empty-state aware
      EmptyState.tsx                 // icon + title + description
      Button.tsx
    charts/
      LineChart.tsx           // forecast, sales trend
      BarChart.tsx             // anomaly losses, sales by day
      DoughnutChart.tsx         // revenue mix
    domain/
      IngredientRow.tsx       // inventory table row incl. Meter
      AlertRow.tsx              // Today > Action Required row
      ExpiryCard.tsx              // Expiry Watch batch card
      VerificationItem.tsx          // Confirm/Change/Keep row
  lib/
    api/                     // one file per domain, see §4
    types/                   // shared TS interfaces, see §3
    severity.ts               // shared fn: daysLeft -> 'crit'|'warn'|'good'
  hooks/
    useIngredients.ts
    useForecast.ts
    useAnomalies.ts
    useReconciliation.ts
    useVerificationQueue.ts
```

Build `components/ui/*` and `components/charts/*` first — every route composes from them. Nothing route-specific belongs in `ui/`.

---

## 3. Shared types (`lib/types/`)

```ts
type Severity = 'crit' | 'warn' | 'good' | 'info' | 'flat';

interface Ingredient {
  id: string;                 // ING-0061
  name: string;
  category: 'Wet food'|'Dry food'|'Consumable'|'Operational supply';
  onHandQty: number;
  onHandUnit: string;         // "packs", "bottles", "g"
  onHandBaseQty: number;      // normalized, e.g. grams/ml
  usedPerDay: number | null;  // null = not enough sales history
  daysLeft: number | null;    // null -> "insufficient data", not 0
  expiryDate: string | null;
  confidence: 'confirmed' | 'unverified';
  costPerUnit: number | null; // null = "missing a cost", never render as ₱0
}

interface StockBatch {
  batchId: string;            // SEED-0070
  ingredientId: string;
  receivedDate: string;
  expiryDate: string | null;
  expirySource: 'exact' | 'estimated' | 'none';
  qtyRemaining: number;
  drawOrder: number;          // 1 = next to be consumed (FEFO)
}

interface StockMovement {
  id: string;
  timestamp: string;
  ingredientId: string;
  reason: 'sale_deduction' | 'order_voided_return' | 'receiving' | 'wastage' | 'stock_count' | 'correction';
  changeQty: number;          // signed
  stockAfter: number;
  linkedOrderId: string | null;
  note: string | null;
}

interface MenuItem {
  id: string;
  name: string;
  price: number;
  sellable: boolean;
  blockingIngredientId: string | null; // why it's unavailable, if not sellable
  recipeId: string;
}

interface Recipe {
  id: string;
  menuItemId: string;
  lines: { ingredientId: string; amount: number; unit: string; cost: number }[];
  costPerServing: number;
  marginPct: number;
}

interface ForecastPoint { date: string; predictedUnits: number; }
interface ForecastSeries {
  menuItemId: string;
  daysOfHistory: number;      // < 7 => insufficient data, show gap message not zeros
  series: ForecastPoint[];
  accuracy: { mae: number; rmse: number; mape: number } | null;
}

interface ReplenishmentRow {
  ingredientId: string;
  onHand: number;
  usedPerDay: number;
  daysLeft: number;
  leadTimeDays: number;
  reorderPoint: number | null;   // null = system-suggested, not manager-set
  suggestedOrderQty: number;
  status: Severity;
}

interface AnomalyFlag {
  id: string;
  ingredientId: string;
  timestamp: string;
  amount: number;
  reasonCode: 'wastage'|'spoilage'|'count_correction';
  flaggedBecause: string;      // human-readable, backend-generated
  priority: 'review_soon' | 'worth_a_look';
}

interface ReconciliationStream {
  stream: 'Unlimited Wings'|'Unlimited Rice'|'Unlimited Fries'|'Unlimited Drinks';
  deductsFrom: string;         // ingredientId
  coefficient: number;         // λ, current rolling
  baselineCoefficient: number; // frozen
  driftPct: number;
  windowDays: number;
}

interface VerificationItem {
  id: string;
  ingredientId: string;
  priority: 1 | 2 | 3;
  field: 'pack_size' | 'yield' | 'recipe_qty' | 'unlimited_rate' | 'cost' | 'expiry';
  question: string;            // backend-authored, plain language
  currentValue: string;
  history: { value: string; changedBy: string; timestamp: string }[];
}
```

**Rule for the frontend team:** never compute `daysLeft`, `status`, or `suggestedOrderQty` client-side. These come from the backend (Prophet/replenishment logic). The frontend only renders severity from the value it's given — see `lib/severity.ts` for the display-only threshold mapping (not a recompute).

---

## 4. Route ↔ endpoint map

| Route | Reads | Notes |
|---|---|---|
| Today | `GET /dashboard/summary` | Aggregation endpoint — do **not** have Today independently query 6 other endpoints and merge client-side. Backend should return one shaped payload: `{ kpis, alerts, forecastPreview, expiryPreview, verifyPreview }`. |
| Inventory / On Hand | `GET /ingredients` | Paginate server-side past ~150 rows. |
| Inventory / Batches | `GET /ingredients/:id/batches` | FEFO order is server-computed (`drawOrder`), never sorted client-side by date alone — expiry source matters. |
| Inventory / Receiving | `POST /receiving` | On success, invalidate `ingredients` and `batches` queries for that ingredient. |
| Inventory / Movements | `GET /ledger?ingredientId=&reason=&page=` | Read-only, append-only — no edit/delete actions in the UI, ever. |
| Menu / Availability | `GET /menu-items` | `blockingIngredientId` drives the "why unavailable" column — don't infer it from stock levels client-side. |
| Menu / Recipes | `GET /recipes/:menuItemId` | |
| Forecasting | `GET /forecast?menuItemId=` | If `daysOfHistory < 7`, render the insufficient-data state — never draw a flat/fabricated line. |
| Replenishment | `GET /replenishment` | |
| Anomaly Detection | `GET /anomalies?window=90d` | |
| Reconciliation | `GET /reconciliation/:date`, `POST /reconciliation/:date/submit` | |
| Expiry Watch | `GET /expiry?window=90d` | |
| Verification | `GET /verification-queue?priority=`, `POST /verification/:id/confirm`, `POST /verification/:id/change` | `change` always appends to `history`, never overwrites. |
| Reports | `GET /reports/sales?range=`, `GET /reports/combos`, `GET /reports/valuation` | |
| System | `GET /system/checks`, `GET /system/model-details` | |

---

## 5. Component contracts (the ones that repeat across routes)

**`<Pill variant severity label />`** — used for every status badge. `severity` drives color, `label` is the text. Don't hardcode Tailwind color classes per usage site; always go through this component so a palette change is one file.

**`<Meter value max severity />`** — the runway/expiry bar. Used in Inventory On Hand (days-left) and Expiry cards (time-to-expiry). Same component, different `max`.

**`<RailCard severity>` ** — wraps `<Card>` and adds the colored left border. Used for every KPI card that represents a status (not for neutral/reference cards).

**`<EmptyState icon title description action? />`** — every "nothing here yet" state (Purchases, Anomalies, Combos, Replenishment-nothing-urgent) uses this, not a one-off `<p>`. Keeps empty-state copy consistent in tone (see prototype: state what's true, then what to do next — never "Oops" or apologetic copy).

**`<Tabbar items activeId onChange />`** — Inventory, Menu, Intelligence, Verification all use the same sub-tab bar. One component, route owns the `items` array and the subview it swaps in.

---

## 6. Behavioral rules to carry over (not just visual)

1. **Today must render without its own scroll on a 1366×768 viewport.** If a new KPI or panel is added later, it replaces one of the five existing ones — it doesn't get appended and pushed off-screen.
2. **Ledger entries are immutable in the UI.** No edit/delete affordance anywhere in Movements, ever — this mirrors the backend's append-only guarantee (§ system spec, "Database/Ledger Integrity").
3. **Paired void/return movements should render adjacent and visually linked** (same order id, opposite sign) — don't let the list re-sort them apart by exact timestamp tie-break.
4. **Never show `null` as `0`.** Missing cost → "no cost set," not ₱0.00. No sales history → "needs 7+ days," not a flat forecast line at 0. This was the exact confusion that triggered this rebuild (a seeded/test "thrown away" entry read as real loss) — treat "no data" and "zero/none" as visually distinct everywhere.
5. **Anomaly flags are informational, not accusatory.** Copy stays factual ("2.4× typical wastage for this item") — no automated language implying theft or blame; that judgment stays with the manager.
6. **Verification changes are additive.** Confirm/Change never rewrites a historical POS transaction — only future deductions use the new value.

---

## 7. Suggested build order for Claude Code

1. Tokens + `components/ui/*` (Card, Pill, Stat, Meter, Tabbar, EmptyState, RailCard, Table)
2. `AppShell` + `Sidebar` + `Topbar` + routing shell (all 8 routes reachable, empty bodies)
3. Today (highest-value screen, forces the shared components to be right early)
4. Inventory (On Hand → Batches → Receiving → Movements, in that order)
5. Intelligence (Forecasting → Replenishment → Anomaly Detection → Reconciliation)
6. Menu, Expiry, Verification, Reports, System (these mostly reuse components built above — should go fast)

Hand this file plus `wings-theory-console.html` to Claude Code together: the HTML is the visual/interaction reference, this doc is the data contract and component boundaries so it builds against your real API instead of guessing field names.
