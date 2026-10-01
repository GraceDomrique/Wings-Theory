/**
 * Plain-restaurant-language wording, in one place, so screens say the same
 * thing everywhere and a rename is one edit. Display strings only - nothing
 * here computes or interprets data.
 *
 * Forecast wording is deliberately hedged: a forecast is "expected", never
 * certain. "Technical details" screens may use the real names.
 */
export const TERMS = {
  expectedDemand: 'Expected demand',
  mayRunOut: 'May run out',
  mayBeBusier: 'May be busier than usual',
  likelyRange: 'Likely range',
  whatToBuy: 'What to buy',
  suggestedPurchase: 'Suggested purchase',
  useFirst: 'Use first',
  unusualUsage: 'Unusual usage',
  stockDifference: 'Stock difference',
  needsChecking: 'Needs checking',
  oftenOrderedTogether: 'Often ordered together',
  stockHistory: 'Stock history',
  activityLog: 'Activity log',
  systemHealth: 'System health',
  technicalDetails: 'Technical details',
  howCalculated: 'How was this calculated?',
  // Margins/costs: only what is actually known (recipe + ingredient cost).
  sellingPrice: 'Selling price',
  ingredientCost: 'Ingredient cost',
  estimatedMargin: 'Estimated margin',
  estimatedFoodCost: 'Estimated food cost',
  foodCostPctOfSales: 'Food cost (% of sales)',
};

/** "Appeared together in X orders" - states only what the count proves. */
export function togetherInOrders(n) {
  const k = Number(n) || 0;
  return `Appeared together in ${k} ${k === 1 ? 'order' : 'orders'}`;
}
