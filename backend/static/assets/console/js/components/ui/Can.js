import { h } from '../../lib/dom.js';
import { can, loadCapabilities } from '../../lib/capabilities.js';

/**
 * Render `build()` only if the signed-in role holds `capability`.
 *
 * UI visibility only - the backend's needs() gate on the endpoint is what
 * actually protects the action. Fails closed: nothing is shown until the
 * published policy has loaded, and nothing if it never does.
 *
 * Returns a host element to drop in place; it fills itself (or stays empty).
 * `display: contents` keeps the host out of layout so it never adds a gap.
 *
 * @param {string} capability e.g. 'purchase_order.create'
 * @param {() => Node} build  builds the control; only called if permitted
 */
export function Can(capability, build) {
  const host = h('span', { style: { display: 'contents' } });
  const fill = () => { if (can(capability)) host.replaceChildren(build()); };
  if (can(capability)) fill();
  else loadCapabilities().then(fill);
  return host;
}
