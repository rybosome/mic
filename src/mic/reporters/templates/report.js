(() => {
  'use strict';

  const {manifest, cases} = JSON.parse(document.getElementById('run-data').textContent);
  const $ = id => document.getElementById(id);
  const tabNames = ['details', 'provenance', 'errors'];
  const json = value => JSON.stringify(value, null, 2);
  const display = value => typeof value === 'string' ? value : json(value);
  const numeric = value => typeof value === 'number' ? value.toFixed(3) : 'unscored';
  let selected = 0;
  let visible = [];
  let activeTab = 'details';

  function text(tag, value, className) {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) element.className = className;
    return element;
  }

  function notice(title, message) {
    const element = text('div', '', 'error');
    element.append(text('strong', title), text('p', message));
    $('run-notices').append(element);
  }

  function stat(label, main, detail) {
    const element = text('div', '', 'stat');
    element.append(text('div', label, 'eyebrow'), text('strong', main), text('small', detail));
    $('stats').append(element);
  }

  function renderSummary() {
    $('run-name').textContent = manifest.name;
    $('run-status').textContent = manifest.status;
    $('run-status').classList.add(manifest.status);
    $('run-meta').textContent = `${manifest.dataset?.name ?? 'Dataset unavailable'} · ${manifest.dataset?.rows ?? 0} rows · ${manifest.options?.trials ?? 1} trial(s) · ${manifest.run_id}`;
    $('raw-manifest').textContent = json(manifest);
    for (const failure of manifest.failures ?? []) {
      if (failure.case_id == null) notice(`${failure.phase} · ${failure.type}`, failure.message);
    }
    for (const [name, result] of Object.entries(manifest.reporting ?? {})) {
      if (['failed', 'cancelled'].includes(result.status)) {
        notice(`Reporting ${result.status} · ${name}`, display(result.error ?? result.message ?? 'Export did not complete.'));
      }
    }
    stat('Executions', `${manifest.counts?.completed ?? 0} / ${manifest.counts?.planned ?? 0}`,
      `${manifest.counts?.failed ?? 0} errors · ${manifest.counts?.cancelled ?? 0} cancelled`);
    for (const [name, score] of Object.entries(manifest.scores ?? {})) {
      stat(name, numeric(score.mean), `${score.count} numeric · ${score.null_count} unscored · ${score.unavailable_count} unavailable`);
    }
    const gates = manifest.gates ?? [];
    stat('Quality gates', gates.length ? `${gates.filter(gate => gate.passed).length} / ${gates.length}` : 'Not set',
      gates.length ? gates.map(gate => gate.expression).join(' · ') : 'Execution status and quality are separate');
  }

  function valueCard(label, value, wide = false) {
    const element = text('div', '', `value-card${wide ? ' wide' : ''}`);
    element.append(text('h3', label), text('pre', value));
    return element;
  }

  function showTab(name, focus = false) {
    activeTab = name;
    document.querySelectorAll('.tab').forEach(button => {
      const active = button.dataset.tab === name;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    for (const tab of tabNames) $(`panel-${tab}`).hidden = tab !== name;
  }

  function renderDetails(row) {
    const grid = text('div', '', 'value-grid');
    grid.append(
      valueCard('Input', display(row.input), true),
      valueCard('Expected', Object.hasOwn(row, 'expected') ? display(row.expected) : 'Missing — no expected value'),
      valueCard('Output', Object.hasOwn(row, 'output') ? display(row.output) : 'Unavailable — see execution status')
    );
    if (Object.hasOwn(row, 'metadata')) grid.append(valueCard('Dataset metadata', display(row.metadata), true));
    if (Object.hasOwn(row, 'task_metadata')) grid.append(valueCard('Task metadata', display(row.task_metadata), true));
    const scores = text('div', '', 'value-card wide');
    scores.append(text('h3', 'Scores'));
    for (const score of row.scores ?? []) {
      const item = text('div', '', 'score-row');
      item.append(text('span', score.name), text('strong', numeric(score.value), 'mono'));
      scores.append(item);
      if (Object.hasOwn(score, 'metadata')) scores.append(text('pre', json(score.metadata)));
    }
    if (!(row.scores ?? []).length) scores.append(text('p', 'No scores available for this execution.', 'muted'));
    grid.append(scores);
    $('panel-details').replaceChildren(grid);
  }

  function renderErrors(row) {
    $('panel-errors').replaceChildren();
    for (const error of row.errors ?? []) {
      const element = text('div', '', 'error');
      element.append(text('strong', `${error.phase} · ${error.type}`), text('p', error.message));
      if (error.scorer) element.append(text('p', `Scorer: ${error.scorer}`));
      if (error.traceback) {
        const details = document.createElement('details');
        details.append(text('summary', 'Traceback'), text('pre', error.traceback));
        element.append(details);
      }
      $('panel-errors').append(element);
    }
    if (!(row.errors ?? []).length) {
      $('panel-errors').append(text('p', 'No execution errors. Score quality is shown independently in Case details.', 'muted'));
    }
  }

  function selectCase(index, focus = false) {
    selected = index;
    const row = cases[index];
    document.querySelectorAll('.case').forEach(button => {
      const active = Number(button.dataset.index) === index;
      button.setAttribute('aria-current', String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $('copy-id').disabled = !row;
    if (!row) {
      $('selected-name').textContent = 'No matching cases';
      $('selected-meta').textContent = 'Adjust the filters to inspect a case.';
      for (const name of tabNames) $(`panel-${name}`).replaceChildren();
      $('raw-case').textContent = '';
      $('announcer').textContent = 'No matching cases';
      return;
    }
    $('selected-name').textContent = row.case_id;
    $('selected-meta').textContent = `${row.status} · row ${row.row_index} · trial ${row.trial} · ${numeric(row.latency?.total_ms)} ms total`;
    $('raw-case').textContent = json(row);
    renderDetails(row);
    $('panel-provenance').replaceChildren(
      valueCard('Case source', json(row.provenance ?? {})),
      valueCard('Dataset snapshot', json(manifest.dataset ?? {})),
      valueCard('Definition & environment', json(manifest.provenance ?? {}))
    );
    renderErrors(row);
    showTab(activeTab);
    $('announcer').textContent = `Selected ${row.case_id}, trial ${row.trial}, ${row.status}`;
  }

  function moveCase(event, index) {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const position = visible.indexOf(index);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1
      : Math.max(0, Math.min(visible.length - 1, position + (event.key === 'ArrowDown' ? 1 : -1)));
    selectCase(visible[next], true);
  }

  function filterCases() {
    const query = $('search').value.toLowerCase();
    const status = $('status-filter').value;
    visible = [];
    $('case-list').replaceChildren();
    cases.forEach((row, index) => {
      if (query && !json(row).toLowerCase().includes(query)) return;
      if (status === 'unscored' && !(row.scores ?? []).some(score => score.value === null)) return;
      if (!['all', 'unscored'].includes(status) && row.status !== status) return;
      visible.push(index);
      const button = text('button', '', 'case');
      button.type = 'button';
      button.dataset.index = index;
      const heading = text('span', '', 'case-id');
      heading.append(text('span', row.case_id), text('span', '●', `dot ${row.status}`));
      const scores = (row.scores ?? []).map(score => `${score.name} ${numeric(score.value)}`).join(' · ') || 'no scores';
      button.append(heading, text('small', `Trial ${row.trial} · ${row.status} · ${scores}`));
      button.addEventListener('click', () => selectCase(index));
      button.addEventListener('keydown', event => moveCase(event, index));
      $('case-list').append(button);
    });
    $('case-count').textContent = `(${visible.length})`;
    if (!visible.length) $('case-list').append(text('p', 'No matching cases.', 'empty'));
    selectCase(visible.includes(selected) ? selected : (visible[0] ?? -1));
  }

  async function copyCaseId() {
    const index = selected;
    const id = String(cases[index]?.case_id ?? '');
    try {
      await navigator.clipboard.writeText(id);
      $('announcer').textContent = 'Case ID copied';
    } catch {
      // The selection fallback is useful for offline file URLs without clipboard permission.
      if (selected !== index) return;
      const selection = window.getSelection();
      if (!selection) {
        $('announcer').textContent = 'Clipboard unavailable. Select and copy the case ID above.';
        return;
      }
      const range = document.createRange();
      range.selectNodeContents($('selected-name'));
      selection.removeAllRanges();
      selection.addRange(range);
      $('announcer').textContent = 'Case ID selected. Press your keyboard copy shortcut.';
    }
  }

  $('search').addEventListener('input', filterCases);
  $('status-filter').addEventListener('change', filterCases);
  $('copy-id').addEventListener('click', copyCaseId);
  for (const button of document.querySelectorAll('.tab')) {
    button.addEventListener('click', () => showTab(button.dataset.tab));
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const position = tabNames.indexOf(activeTab);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabNames.length - 1
        : (position + (event.key === 'ArrowRight' ? 1 : tabNames.length - 1)) % tabNames.length;
      showTab(tabNames[next], true);
    });
  }
  renderSummary();
  filterCases();
})();
