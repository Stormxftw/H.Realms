(() => {
  'use strict';

  function apiPath(path, base = '') {
    const cleanBase = String(base || '').replace(/\/+$/, '');
    const cleanPath = String(path || '').startsWith('/') ? String(path) : `/${path}`;
    return cleanBase ? `${cleanBase}${cleanPath}` : cleanPath;
  }

  function controlState(control) {
    const value = control.value ?? null;
    const displayValue = value === null || value === undefined
      ? 'Not available'
      : `${value}${control.unit ? ` ${control.unit}` : ''}`;
    return { id: control.id, value, displayValue };
  }

  function isControlEnabled(control, online) {
    if (control.disabled === true) return false;
    if (control.enabledWhen === 'online') return online === true;
    if (control.enabledWhen === 'offline') return online === false;
    return true;
  }

  function statusPresentation(game = {}, service = {}) {
    const presentations = {
      running_ready: { label: 'RUNNING', tone: 'online' },
      running_degraded: { label: 'DEGRADED', tone: 'degraded' },
      stopped: { label: 'STOPPED', tone: 'offline' },
      not_installed: { label: 'SETUP NEEDED', tone: 'setup' },
      unknown: { label: 'UNKNOWN', tone: 'unknown' },
    };
    const inferred = game.projectPresent === false || game.readiness === 'needs_setup'
      ? 'not_installed'
      : 'unknown';
    const state = presentations[service.state] ? service.state : inferred;
    const reasons = [];
    for (const blocker of game.blockers || service.blockers || []) {
      if (blocker?.message) reasons.push(blocker.message);
    }
    if (service.process?.error) reasons.push(service.process.error);
    for (const listener of service.listeners || []) {
      if (listener.error) reasons.push(listener.error);
      else if (listener.ok === true && listener.listening === false && service.process?.running === true) {
        reasons.push(`No ${String(listener.protocol || '').toUpperCase()} listener detected on port ${listener.port}.`);
      }
    }
    if (service.query?.error) reasons.push(service.query.error);
    if (state === 'unknown' && reasons.length === 0) reasons.push('Status probes did not return a result.');
    return { state, ...presentations[state], reasons: [...new Set(reasons)] };
  }

  function confirmationCopy(plan) {
    const before = plan.currentValue === null || plan.currentValue === undefined
      ? 'current state'
      : JSON.stringify(plan.currentValue);
    const after = plan.proposedValue === null || plan.proposedValue === undefined
      ? plan.controlLabel
      : JSON.stringify(plan.proposedValue);
    const restart = plan.restartRequired
      ? ' A server restart is required before this takes effect.'
      : '';
    return `${plan.gameName}: ${plan.controlLabel}. Change ${before} to ${after}. Risk: ${plan.risk}.${restart}`;
  }

  const exported = { apiPath, controlState, isControlEnabled, statusPresentation, confirmationCopy };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  if (typeof document === 'undefined') return;

  const API_BASE = document.documentElement.dataset.apiBase || '';
  const state = {
    status: null,
    catalog: null,
    selectedGameId: localStorage.getItem('hermes-game-host-selected') || 'minecraft',
    pendingPlan: null,
    refreshing: false,
    activity: [],
  };

  const $ = (selector) => document.querySelector(selector);
  const gameList = $('#game-list');
  const controlsRoot = $('#controls-root');
  const activityRoot = $('#activity-root');
  const dialog = $('#confirm-dialog');
  const confirmCopyNode = $('#confirm-copy');
  const confirmTitleNode = $('#confirm-title');
  const applyButton = $('#confirm-apply');
  const cancelButton = $('#confirm-cancel');

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  async function api(path, options = {}) {
    const response = await fetch(apiPath(path, API_BASE), {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      cache: 'no-store',
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function serviceFor(gameId) {
    return state.status?.services?.[gameId] || null;
  }

  function profileFor(gameId) {
    return state.catalog?.games?.find((game) => game.id === gameId) || null;
  }

  function installedGames() {
    return (state.catalog?.games || []).filter((game) => game.installed === true);
  }

  function setConnectionState(kind, text) {
    const dot = $('#gateway-dot');
    dot.className = `status-dot ${kind}`;
    $('#gateway-text').textContent = text;
  }

  function showToast(message, tone = 'ok') {
    const toast = element('div', `toast ${tone}`, message);
    $('#toast-stack').appendChild(toast);
    window.setTimeout(() => toast.remove(), 5200);
  }

  function recordActivity(title, detail, tone = 'ok') {
    state.activity.unshift({ title, detail, tone, at: new Date() });
    state.activity = state.activity.slice(0, 8);
    renderActivity();
  }

  function renderActivity() {
    activityRoot.replaceChildren();
    if (!state.activity.length) {
      activityRoot.appendChild(element('p', 'empty-copy', 'No controls used in this session.'));
      return;
    }
    for (const item of state.activity) {
      const row = element('div', 'activity-row');
      row.appendChild(element('span', `activity-mark ${item.tone}`));
      const copy = element('div', 'activity-copy');
      copy.appendChild(element('strong', '', item.title));
      copy.appendChild(element('span', '', item.detail));
      row.append(copy, element('time', '', item.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));
      activityRoot.appendChild(row);
    }
  }

  function renderGameList() {
    gameList.replaceChildren();
    const games = installedGames();
    if (games.length === 0) {
      gameList.appendChild(
        element('p', 'empty-copy', 'No servers installed. Add one from the Store in the Desktop plugin.'),
      );
      return;
    }
    for (const game of games) {
      const service = serviceFor(game.id);
      const presentation = statusPresentation(game, service || {});
      const button = element('button', `game-row${game.id === state.selectedGameId ? ' active' : ''}`);
      button.type = 'button';
      button.addEventListener('click', () => {
        state.selectedGameId = game.id;
        localStorage.setItem('hermes-game-host-selected', game.id);
        render();
      });
      const identity = element('span', 'game-identity');
      identity.append(element('span', `status-dot ${presentation.tone}`));
      const labels = element('span', 'game-labels');
      labels.append(element('strong', '', game.name), element('small', '', presentation.label));
      identity.appendChild(labels);
      button.append(identity, element('span', 'chevron', '›'));
      gameList.appendChild(button);
    }
  }

  function renderHero(game, service) {
    const presentation = statusPresentation(game, service || {});
    $('#game-name').textContent = game.name;
    $('#game-description').textContent = game.description || 'Typed controls managed by Hermes.';
    const stateBadge = $('#server-state');
    stateBadge.textContent = presentation.label;
    stateBadge.className = `state-badge ${presentation.tone}`;
    const reasons = $('#status-reasons');
    reasons.replaceChildren(...presentation.reasons.map(reason => element('p', '', reason)));
    reasons.hidden = presentation.reasons.length === 0;
    $('#last-refresh').textContent = state.status?.generatedAt
      ? new Date(state.status.generatedAt).toLocaleTimeString()
      : '—';

    const connect = service?.connect || {};
    $('#connect-value').textContent = connect.lan || connect.local || connect.public || 'Unavailable';
    const process = service?.process || {};
    $('#uptime-value').textContent = process.uptimeHuman || '—';
    const players = service?.players;
    $('#players-value').textContent = players ? `${players.online ?? 0} / ${players.max ?? 0}` : '—';
    $('#memory-value').textContent = process.rssMB ? `${process.rssMB} MB` : '—';
  }

  function riskLabel(risk) {
    return {
      'read-only': 'Read only',
      'safe': 'Safe',
      'safe-mutation': 'Safe change',
      'configuration': 'Configuration',
      'service': 'Service action',
      'disruptive': 'Disruptive',
    }[risk] || risk || 'Unknown';
  }

  function controlHeader(control) {
    const header = element('div', 'control-head');
    const copy = element('div', 'control-title');
    copy.append(element('label', '', control.label));
    if (control.help) copy.appendChild(element('small', '', control.help));
    header.append(copy, element('span', `risk-chip ${control.risk || 'safe'}`, riskLabel(control.risk)));
    return header;
  }

  function previewButton(control, getValue, online) {
    const button = element('button', 'button secondary', 'Preview change');
    button.type = 'button';
    button.disabled = !isControlEnabled(control, online);
    button.addEventListener('click', () => requestPlan(control, getValue()));
    return button;
  }

  function renderControl(control, online) {
    const card = element('article', `control-card kind-${control.kind}`);
    card.appendChild(controlHeader(control));
    const body = element('div', 'control-body');
    const enabled = isControlEnabled(control, online);

    if (control.kind === 'button') {
      const button = element('button', `button ${control.variant || 'default'}`, control.label);
      button.type = 'button';
      button.disabled = !enabled;
      button.addEventListener('click', () => {
        if (control.binding?.action === 'ui.refresh') load(true);
        else requestPlan(control, null);
      });
      body.appendChild(button);
    } else if (control.kind === 'slider') {
      const row = element('div', 'slider-row');
      const input = element('input', 'range-input');
      input.type = 'range';
      input.min = String(control.min);
      input.max = String(control.max);
      input.step = String(control.step || 1);
      input.value = String(control.value ?? control.min);
      input.disabled = !enabled;
      const output = element('output', 'range-value');
      const syncOutput = () => { output.textContent = `${input.value}${control.unit ? ` ${control.unit}` : ''}`; };
      input.addEventListener('input', syncOutput);
      syncOutput();
      row.append(input, output);
      body.append(row, previewButton(control, () => Number(input.value), online));
    } else if (control.kind === 'switch') {
      const row = element('div', 'switch-row');
      const input = element('input', 'switch-input');
      input.type = 'checkbox';
      input.checked = control.value === true;
      input.disabled = !enabled;
      const visual = element('span', 'switch-visual');
      const label = element('span', 'switch-state', input.checked ? 'Enabled' : 'Disabled');
      input.addEventListener('change', () => { label.textContent = input.checked ? 'Enabled' : 'Disabled'; });
      const toggle = element('label', 'switch-control');
      toggle.append(input, visual, label);
      row.append(toggle, previewButton(control, () => input.checked, online));
      body.appendChild(row);
    } else if (control.kind === 'select') {
      const select = element('select', 'field');
      select.disabled = !enabled;
      for (const option of control.options || []) {
        const node = element('option', '', option.label);
        node.value = String(option.value);
        node.selected = option.value === control.value;
        select.appendChild(node);
      }
      body.append(select, previewButton(control, () => select.value, online));
    } else if (control.kind === 'text' || control.kind === 'number') {
      const input = element('input', 'field');
      input.type = control.kind === 'number' ? 'number' : 'text';
      input.value = control.value ?? '';
      input.disabled = !enabled;
      if (control.maxLength) input.maxLength = control.maxLength;
      if (control.min !== undefined) input.min = String(control.min);
      if (control.max !== undefined) input.max = String(control.max);
      body.append(input, previewButton(control, () => control.kind === 'number' ? Number(input.value) : input.value, online));
    } else {
      body.appendChild(element('div', 'readonly-value', controlState(control).displayValue));
    }

    if (control.restartRequired) body.appendChild(element('p', 'restart-note', 'Takes effect after a server restart.'));
    if (!enabled && control.disabledReason) body.appendChild(element('p', 'disabled-reason', control.disabledReason));
    card.appendChild(body);
    return card;
  }

  function renderControls(game, service) {
    controlsRoot.replaceChildren();
    const groups = new Map();
    for (const control of game.controls || []) {
      const name = control.group || 'Controls';
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(control);
    }
    for (const [name, controls] of groups) {
      const section = element('section', 'control-section');
      section.appendChild(element('h3', '', name));
      const grid = element('div', 'control-grid');
      for (const control of controls) grid.appendChild(renderControl(control, service?.online === true));
      section.appendChild(grid);
      controlsRoot.appendChild(section);
    }
  }

  function render() {
    const game = profileFor(state.selectedGameId) || installedGames()[0];
    if (!game) return;
    state.selectedGameId = game.id;
    const service = serviceFor(game.id);
    renderGameList();
    renderHero(game, service);
    renderControls(game, service);
    renderActivity();
  }

  async function requestPlan(control, value) {
    try {
      const plan = await api('/api/control/plan', {
        method: 'POST',
        body: JSON.stringify({
          gameId: state.selectedGameId,
          controlId: control.id,
          value,
          actor: 'hermes-desktop-ui',
        }),
      });
      state.pendingPlan = plan;
      confirmTitleNode.textContent = plan.risk === 'disruptive' ? 'Confirm disruptive action' : 'Review proposed action';
      confirmCopyNode.textContent = confirmationCopy(plan);
      applyButton.textContent = plan.risk === 'disruptive' ? 'Confirm and run' : 'Apply change';
      applyButton.className = `button ${plan.risk === 'disruptive' ? 'destructive' : 'default'}`;
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    } catch (error) {
      showToast(error.message, 'error');
      recordActivity('Preview failed', error.message, 'error');
    }
  }

  async function applyPlan() {
    if (!state.pendingPlan) return;
    const plan = state.pendingPlan;
    applyButton.disabled = true;
    applyButton.textContent = 'Working…';
    try {
      const result = await api('/api/control/apply', {
        method: 'POST',
        body: JSON.stringify({
          planId: plan.planId,
          planDigest: plan.planDigest,
          confirmed: true,
          actor: 'hermes-desktop-ui',
        }),
      });
      const detail = result.output || `${plan.controlLabel} completed.`;
      recordActivity(plan.controlLabel, detail, 'ok');
      showToast(`${plan.controlLabel} completed.`, 'ok');
      closeDialog();
      await load(true);
    } catch (error) {
      showToast(error.message, 'error');
      recordActivity(`${plan.controlLabel} failed`, error.message, 'error');
      applyButton.disabled = false;
      applyButton.textContent = 'Try again';
    }
  }

  function closeDialog() {
    state.pendingPlan = null;
    applyButton.disabled = false;
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  async function load(showSuccess = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    $('#refresh-button').disabled = true;
    setConnectionState('loading', 'Refreshing');
    try {
      const [status, catalog] = await Promise.all([api('/api/status'), api('/api/controls')]);
      state.status = status;
      state.catalog = catalog;
      setConnectionState('online', 'Console connected');
      render();
      if (showSuccess) showToast('Status refreshed.', 'ok');
    } catch (error) {
      setConnectionState('offline', 'Console unavailable');
      showToast(error.message, 'error');
      recordActivity('Connection failed', error.message, 'error');
    } finally {
      state.refreshing = false;
      $('#refresh-button').disabled = false;
    }
  }

  $('#refresh-button').addEventListener('click', () => load(true));
  cancelButton.addEventListener('click', closeDialog);
  applyButton.addEventListener('click', applyPlan);
  dialog.addEventListener('cancel', (event) => { event.preventDefault(); closeDialog(); });
  load(false);
  window.setInterval(() => {
    if (!dialog.open && document.visibilityState === 'visible') load(false);
  }, 10_000);
})();
