import { h } from '../../lib/dom.js';
import { Card, CardHeader } from '../../components/ui/Card.js';
import { Table } from '../../components/ui/Table.js';
import { Pill } from '../../components/ui/Pill.js';
import { Modal } from '../../components/ui/Modal.js';
import { EmptyState } from '../../components/ui/EmptyState.js';
import { stamp } from '../../lib/format.js';
import { getDataQuality } from '../../lib/api/system.js';

/**
 * Administration > Data Integrity — spec section 19: "Is the stored system
 * data structurally consistent?" GET /console/data-quality
 * (wt/data_quality.py), 12 deterministic SQL checks over raw tables
 * (duplicate orders, orphaned rows, missing links, invalid quantities).
 * Deliberately separate from Setup & Verification: that page is
 * placeholder VALUES a person confirms; this is structural correctness of
 * the data itself, found by code — the two are unrelated even though their
 * backend module names (data_quality.py vs. verification.py) invite
 * confusing them.
 */
export async function render(ctx) {
  const report = await getDataQuality();

  return Card({},
    CardHeader({
      title: 'Data integrity audit',
      desc: `${report.checksWithFindings} of ${report.checksRun} checks found something — automatic structural checks over the raw tables`,
      right: h('div', { class: 't-xs t-faint' }, 'as of ', stamp(report.generatedAt)),
    }),
    Table({
      columns: [
        { key: 'check', label: 'Check', render: (c) => h('span', { class: 'mono t-xs' }, c.check) },
        { key: 'description', label: 'Description', render: (c) => h('span', { class: 't-sm' }, c.description) },
        { key: 'count', label: 'Found', render: (c) => h('span', { class: 'mono t-sm' }, String(c.count)) },
        {
          key: 'status', label: 'Status',
          render: (c) => c.status === 'ok'
            ? Pill('good', 'passed')
            : h('div', { style: { display: 'flex', gap: '8px', alignItems: 'center' } },
              Pill('warn', `needs attention · ${c.count} found`),
              c.examples.length ? h('a', { href: '#', class: 't-xs', onClick: (e) => { e.preventDefault(); openExamplesModal(c); } }, 'examples') : null),
        },
      ],
      rows: report.checks,
      empty: EmptyState({ icon: '✓', title: 'No checks configured' }),
    }));
}

function openExamplesModal(check) {
  const rows = check.examples.map((ex) => h('div', { style: { padding: '8px 0', borderBottom: '1px solid var(--line-soft)' } },
    h('div', { class: 't-xs mono' }, Object.entries(ex).map(([k, v]) => `${k}: ${v}`).join('  ·  '))));
  Modal({ title: check.check, desc: check.description, onClose: () => {} },
    rows.length ? h('div', {}, ...rows) : EmptyState({ icon: '—', title: 'No example rows', tight: true }));
}
