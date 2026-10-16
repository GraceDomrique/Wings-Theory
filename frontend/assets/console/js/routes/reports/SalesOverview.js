import { session } from '../../lib/api/client.js';
import * as OwnerSales from './OwnerSales.js';
import * as AdminSales from './AdminSales.js';

/**
 * Route id 'salesoverview' is shared by both roles (nav.js: labeled "Sales
 * Overview" for Admin, "Sales" for Owner). As of Phase 2 (2026-10) the two
 * roles see genuinely different pages, same split as Today/Overview: Owner
 * gets the full strategic business-intelligence screen (OwnerSales.js —
 * trend, drivers, category/customer patterns, FP-Growth, business rhythm),
 * Admin gets the operational summary + orders screen (AdminSales.js — see
 * that module's own docstring for why it is deliberately narrower).
 */
export async function render(ctx) {
  const role = (session.user || {}).role;
  return role === 'owner' ? OwnerSales.render(ctx) : AdminSales.render(ctx);
}
