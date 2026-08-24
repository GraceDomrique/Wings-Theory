import { h, mount } from '../../lib/dom.js';
import { Tabbar } from '../../components/ui/Tabbar.js';
import { Loading, ErrorState } from '../../components/ui/Feedback.js';
import { destroyAllCharts } from '../../components/charts/base.js';

import * as Forecasting from './Forecasting.js';
import * as Replenishment from './Replenishment.js';
import * as AnomalyDetection from './AnomalyDetection.js';
import * as Reconciliation from './Reconciliation.js';
import * as Variance from './Variance.js';

const TABS = [
  { id: 'forecast', label: 'Forecasting' },
  { id: 'replenish', label: 'Replenishment' },
  { id: 'anomaly', label: 'Anomaly Detection' },
  { id: 'recon', label: 'Reconciliation' },
  { id: 'variance', label: 'Ingredient Variance' },
];

const SUBVIEWS = {
  forecast: Forecasting,
  replenish: Replenishment,
  anomaly: AnomalyDetection,
  recon: Reconciliation,
  variance: Variance,
};

export async function render(ctx) {
  const active = SUBVIEWS[ctx.tab] ? ctx.tab : 'forecast';
  const body = h('div');

  const shell = h('div', {},
    Tabbar({
      items: TABS,
      activeId: active,
      onChange: (id) => {
        history.replaceState(null, '', `#/intel/${id}`);
        drawSub(id);
      },
    }),
    body);

  async function drawSub(id) {
    // Forecasting and Anomaly both own charts; leaving the old instances alive
    // across a tab swap leaves the next one blank.
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
