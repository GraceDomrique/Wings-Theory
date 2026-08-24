import { h, mount } from '../../lib/dom.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Loading, ErrorState } from '../../components/ui/Feedback.js';
import { destroyAllCharts } from '../../components/charts/base.js';

import * as MenuAvailability from './MenuAvailability.js';
import * as Recipes from './Recipes.js';

const TABS = [
  { id: 'avail', label: 'Menu Availability' },
  { id: 'recipes', label: 'Recipes' },
];

const SUBVIEWS = { avail: MenuAvailability, recipes: Recipes };

export async function render(ctx) {
  const active = SUBVIEWS[ctx.tab] ? ctx.tab : 'avail';
  const body = h('div');

  const shell = h('div', {},
    Tabbar({
      items: TABS,
      activeId: active,
      onChange: (id) => {
        history.replaceState(null, '', `#/menu/${id}`);
        drawSub(id);
      },
    }),
    body);

  async function drawSub(id) {
    destroyAllCharts();
    mount(body, Loading());
    try {
      mount(body, await SUBVIEWS[id].render(ctx));
    } catch (err) {
      mount(body, ErrorState({ message: err.message, onRetry: () => drawSub(id) }));
    }
  }

  await drawSub(active);
  return shell;
}
