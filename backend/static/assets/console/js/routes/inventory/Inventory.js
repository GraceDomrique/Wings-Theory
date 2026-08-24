import { h, mount } from '../../lib/dom.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Loading, ErrorState } from '../../components/ui/Feedback.js';
import { destroyAllCharts } from '../../components/charts/base.js';

import * as OnHand from './OnHand.js';
import * as Batches from './Batches.js';
import * as Receiving from './Receiving.js';
import * as Movements from './Movements.js';

/**
 * Inventory tab shell. The route owns the `items` array and swaps the subview;
 * Tabbar is the shared component (spec section 5).
 *
 * `selection` is the one piece of state shared across sub-tabs: clicking a row
 * in On Hand carries that ingredient into Batches and Movements instead of
 * making the user find it again.
 */
const TABS = [
  { id: 'onhand', label: 'On Hand' },
  { id: 'batches', label: 'Batches (FEFO)' },
  { id: 'receive', label: 'Receiving' },
  { id: 'moves', label: 'Movements' },
];

const SUBVIEWS = { onhand: OnHand, batches: Batches, receive: Receiving, moves: Movements };

/** Shared across sub-tabs and across visits to Inventory within a session. */
export const selection = { ingredient: null };

export async function render(ctx) {
  const active = SUBVIEWS[ctx.tab] ? ctx.tab : 'onhand';
  const body = h('div');

  const shell = h('div', {},
    Tabbar({
      items: TABS,
      activeId: active,
      onChange: (id) => {
        // Sub-tabs are real routes, so a batch view can be linked to and
        // survives a reload.
        history.replaceState(null, '', `#/inventory/${id}`);
        drawSub(id);
      },
    }),
    body);

  async function drawSub(id) {
    destroyAllCharts();
    mount(body, Loading());
    try {
      const view = await SUBVIEWS[id].render({
        ...ctx,
        selection,
        openIngredient: (ing) => {
          selection.ingredient = ing;
          history.replaceState(null, '', '#/inventory/batches');
          for (const btn of shell.querySelector('.tabbar').children) {
            btn.classList.toggle('active', btn.textContent === 'Batches (FEFO)');
          }
          drawSub('batches');
        },
        showMovements: (ing) => {
          selection.ingredient = ing;
          history.replaceState(null, '', '#/inventory/moves');
          for (const btn of shell.querySelector('.tabbar').children) {
            btn.classList.toggle('active', btn.textContent === 'Movements');
          }
          drawSub('moves');
        },
      });
      mount(body, view);
    } catch (err) {
      mount(body, ErrorState({ message: err.message, onRetry: () => drawSub(id) }));
    }
  }

  await drawSub(active);
  return shell;
}
