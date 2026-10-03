import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
let JSDOM;
try {
  ({ JSDOM } = await import('jsdom'));
} catch (error) {
  console.error('Report interaction tests require dev dependencies. Run: npm ci --ignore-scripts --prefix tests/reporting/js');
  throw error;
}

const templateUrl = new URL('../../../src/mic/reporters/templates/report.html', import.meta.url);
const scriptUrl = new URL('../../../src/mic/reporters/templates/report.js', import.meta.url);
const template = fs.readFileSync(templateUrl, 'utf8');
const script = new vm.Script(fs.readFileSync(scriptUrl, 'utf8'), { filename: scriptUrl.pathname });

function fixture() {
  return {
    manifest: {
      run_id: 'run-example', status: 'failed',
      summary: {
        tasks: {'<img src=x onerror=alert(1)>': {
          scores: {exact: {mean: 0, count: 1, min: 0, max: 0}}, trials: {}
        }},
        trials: {completed: 1, planned: 3, task_failed: 0, scoring_failed: 1, cancelled: 1}
      },
      sources: {source: {name: 'Synthetic tickets', records_seen: 3, exhausted: true, digest: 'sha256:example'}},
      requirements: [{expression: 'trials.task_failed == 0', passed: false}],
      info: {tasks: {}},
      failures: [{phase: 'setup', type: 'DatasetError', message: 'Setup warning'}],
      sinks: [
        {name: 'braintrust', status: 'failed', error: {message: 'Upload rejected'}},
        {name: 'second', status: 'cancelled'},
      ],
    },
    cases: [
      {
        source_id: 'source', task: 'test', case_id: 'missing', row_index: 0, trial: 1, status: 'completed', input: { text: 'Alpha' },
        output: null, scores: [{ name: 'exact', value: null, metadata: null }], errors: [],
        provenance: { line: 1 }, latency: { total_ms: 3 }, metadata: false, task_metadata: 0,
      },
      {
        source_id: 'source', task: 'test', case_id: 'failed', row_index: 1, trial: 1, status: 'scoring_failed', input: 'Beta', expected: null,
        scores: [{ name: 'exact', value: 0 }], provenance: { line: 2 },
        errors: [{ phase: 'scorer', type: 'ValueError', message: '<script>evil()</script>',
          scorer: 'exact', traceback: 'Traceback: fixture.py:42' }],
      },
      {
        source_id: 'source', task: 'test', case_id: 'cancelled', row_index: 2, trial: 1, status: 'cancelled', input: 'Gamma',
        scores: [], errors: [], provenance: { provider: 'memory' },
      },
    ],
  };
}

function mount(t, payload = fixture()) {
  // Only our trusted source executes. Embedded scripts and resource loading remain disabled;
  // synthetic JSON enters through textContent, never as executable HTML.
  const dom = new JSDOM(template, { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const { window } = dom;
  const { document } = window;
  document.getElementById('run-data').textContent = JSON.stringify(payload);
  script.runInContext(dom.getInternalVMContext());
  return {
    window, document,
    get: id => document.getElementById(id),
    buttons: () => [...document.querySelectorAll('.case')],
    key: (element, key) => element.dispatchEvent(new window.KeyboardEvent('keydown', { key, cancelable: true })),
    search: value => {
      document.getElementById('search').value = value;
      document.getElementById('search').dispatchEvent(new window.Event('input'));
    },
    filter: value => {
      document.getElementById('status-filter').value = value;
      document.getElementById('status-filter').dispatchEvent(new window.Event('change'));
    },
  };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('summary and case values render as text; zero, null and missing retain meaning', t => {
  const ui = mount(t);
  assert.equal(ui.get('run-name').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(ui.document.querySelectorAll('img').length, 0);
  assert.equal(ui.get('run-status').textContent, 'failed');
  assert.match(ui.get('stats').textContent, /exact0\.0001 numeric · min 0\.000 · max 0\.000/);
  assert.match(ui.get('stats').textContent, /Quality gates0 \/ 1trials.task_failed == 0/);
  assert.equal(ui.get('selected-name').textContent, 'missing');
  assert.match(ui.get('panel-details').textContent, /ExpectedMissing — no expected valueOutputnull/);
  assert.match(ui.get('panel-details').textContent, /Dataset metadatafalseTask metadata0/);
  assert.match(ui.get('panel-details').textContent, /exactunscorednull/);
  assert.equal(ui.buttons()[0].getAttribute('aria-current'), 'true');
  assert.equal(ui.buttons()[1].tabIndex, -1);
  assert.equal(ui.window.selectCase, undefined, 'internal controller must not leak globals');
  ui.buttons()[1].click();
  assert.match(ui.get('panel-details').textContent, /ExpectednullOutputUnavailable/);
  assert.equal(JSON.parse(ui.get('raw-case').textContent).expected, null);
  assert.equal(Object.hasOwn(JSON.parse(ui.get('raw-manifest').textContent), 'requirements'), true);
});

test('case arrows and endpoints move selection and keyboard focus within the visible list', t => {
  const ui = mount(t);
  ui.buttons()[0].focus();
  ui.key(ui.buttons()[0], 'ArrowDown');
  assert.equal(ui.get('selected-name').textContent, 'failed');
  assert.equal(ui.document.activeElement, ui.buttons()[1]);
  assert.equal(ui.buttons()[0].tabIndex, -1);
  ui.key(ui.buttons()[1], 'End');
  assert.equal(ui.document.activeElement, ui.buttons()[2]);
  ui.key(ui.buttons()[2], 'ArrowDown');
  assert.equal(ui.document.activeElement, ui.buttons()[2]);
  ui.key(ui.buttons()[2], 'Home');
  ui.key(ui.buttons()[0], 'ArrowUp');
  assert.equal(ui.document.activeElement, ui.buttons()[0]);
  assert.equal(ui.key(ui.buttons()[0], 'Escape'), true, 'unhandled keys are not suppressed');
});

test('search includes raw case evidence and composes with status filters, preserving selection', t => {
  const ui = mount(t);
  ui.buttons()[1].click();
  ui.search('BETA');
  assert.equal(ui.get('case-count').textContent, '(1)');
  assert.equal(ui.get('selected-name').textContent, 'failed');
  ui.filter('completed');
  assert.equal(ui.get('case-count').textContent, '(0)');
  assert.equal(ui.get('selected-name').textContent, 'No matching cases');
  assert.equal(ui.get('copy-id').disabled, true);
  assert.equal(ui.get('panel-errors').textContent, '');
  assert.equal(ui.get('raw-case').textContent, '');
  assert.equal(ui.get('announcer').textContent, 'No matching cases');
  ui.search('');
  assert.equal(ui.get('selected-name').textContent, 'missing');
  ui.filter('unscored');
  assert.deepEqual(ui.buttons().map(button => button.dataset.index), ['0']);
  ui.filter('cancelled');
  assert.equal(ui.get('selected-name').textContent, 'cancelled');
  ui.filter('all');
  assert.equal(ui.get('selected-name').textContent, 'cancelled');
  ui.search('fixture.py:42');
  assert.equal(ui.get('selected-name').textContent, 'failed');
});

test('tabs have roving focus, wrap with arrows, and retain selection across case changes', t => {
  const ui = mount(t);
  ui.get('tab-provenance').click();
  assert.equal(ui.get('panel-provenance').hidden, false);
  assert.equal(ui.get('panel-details').hidden, true);
  assert.match(ui.get('panel-provenance').textContent, /sha256:example/);
  assert.match(ui.get('panel-provenance').textContent, /"line": 1/);
  ui.key(ui.get('tab-provenance'), 'ArrowRight');
  assert.equal(ui.document.activeElement, ui.get('tab-errors'));
  ui.key(ui.get('tab-errors'), 'ArrowRight');
  assert.equal(ui.document.activeElement, ui.get('tab-details'));
  ui.key(ui.get('tab-details'), 'ArrowLeft');
  assert.equal(ui.document.activeElement, ui.get('tab-errors'));
  ui.key(ui.get('tab-errors'), 'Home');
  ui.key(ui.get('tab-details'), 'End');
  ui.buttons()[1].click();
  assert.equal(ui.get('tab-errors').getAttribute('aria-selected'), 'true');
  assert.equal(ui.get('panel-errors').hidden, false);
  assert.match(ui.get('panel-errors').textContent, /scorer · ValueError<script>evil\(\)<\/script>Scorer: exactTraceback/);
  assert.match(ui.get('panel-errors').querySelector('details pre').textContent, /fixture.py:42/);
  assert.equal(ui.get('panel-errors').querySelectorAll('script').length, 0);
  assert.equal(ui.get('tab-details').tabIndex, -1);
});

test('run notices include setup, failed and cancelled reporting without duplicating case errors', t => {
  const ui = mount(t);
  const notices = ui.get('run-notices').textContent;
  assert.match(notices, /Setup warning/);
  assert.match(notices, /Reporting failed · braintrust/);
  assert.match(notices, /Upload rejected/);
  assert.match(notices, /Reporting cancelled · second/);
  assert.doesNotMatch(notices, /Case failure/);
  assert.match(ui.get('panel-errors').textContent, /No execution errors/);
});

test('copy uses the clipboard when permitted', async t => {
  const ui = mount(t);
  const values = [];
  Object.defineProperty(ui.window.navigator, 'clipboard', { value: { writeText: async value => values.push(value) } });
  ui.buttons()[1].click();
  ui.get('copy-id').click();
  await settle();
  assert.deepEqual(values, ['failed']);
  assert.equal(ui.get('announcer').textContent, 'Case ID copied');
});

test('copy falls back to a selectable case ID when clipboard permission is unavailable', async t => {
  const ui = mount(t);
  ui.get('copy-id').click();
  await settle();
  assert.equal(ui.window.getSelection().toString(), 'missing');
  assert.match(ui.get('announcer').textContent, /Press your keyboard copy shortcut/);
});

test('copy failure remains actionable if text selection is unavailable', async t => {
  const ui = mount(t);
  ui.window.getSelection = () => null;
  ui.get('copy-id').click();
  await settle();
  assert.match(ui.get('announcer').textContent, /Clipboard unavailable/);
});

test('clipboard rejection after navigation does not select a different case ID', async t => {
  const ui = mount(t);
  let reject;
  Object.defineProperty(ui.window.navigator, 'clipboard', { value: { writeText: () => new Promise((_, rejectPromise) => { reject = rejectPromise; }) } });
  ui.get('copy-id').click();
  ui.buttons()[1].click();
  reject(new Error('Permission denied'));
  await settle();
  assert.equal(ui.window.getSelection().toString(), '');
  assert.match(ui.get('announcer').textContent, /Selected failed/);
});

test('empty failed runs remain inspectable, with no stale selection or enabled copy action', t => {
  const payload = fixture();
  payload.cases = [];
  payload.manifest.summary.tasks = {};
  payload.manifest.requirements = [];
  const ui = mount(t, payload);
  assert.equal(ui.buttons().length, 0);
  assert.equal(ui.get('copy-id').disabled, true);
  assert.match(ui.get('stats').textContent, /Quality gatesNot set/);
  assert.match(ui.get('raw-manifest').textContent, /Setup warning/);
  ui.get('tab-errors').click();
  assert.equal(ui.get('panel-errors').hidden, false);
});


test('shared case coordinates remain distinguishable by task', t => {
  const payload = fixture();
  payload.cases = [payload.cases[0], {...payload.cases[0], task: 'candidate'}];
  const ui = mount(t, payload);
  assert.match(ui.buttons()[0].textContent, /test · Trial 1/);
  assert.match(ui.buttons()[1].textContent, /candidate · Trial 1/);
  ui.buttons()[1].click();
  assert.match(ui.get('selected-meta').textContent, /candidate · completed/);
  ui.search('candidate');
  assert.equal(ui.buttons().length, 1);
});
