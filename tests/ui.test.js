const assert = require('node:assert/strict');
const ui = require('../static/app.js');

assert.equal(ui.apiPath('/api/status', ''), '/api/status');
assert.equal(
  ui.apiPath('/api/status', '/api/plugins/game-host-console/proxy/'),
  '/api/plugins/game-host-console/proxy/api/status',
);

const slider = {
  id: 'max-players',
  kind: 'slider',
  label: 'Maximum players',
  value: 10,
  min: 1,
  max: 50,
  step: 1,
  unit: 'players',
};

assert.deepEqual(ui.controlState(slider), {
  id: 'max-players',
  value: 10,
  displayValue: '10 players',
});
assert.equal(ui.isControlEnabled({ enabledWhen: 'online' }, true), true);
assert.equal(ui.isControlEnabled({ enabledWhen: 'online' }, false), false);
assert.equal(ui.isControlEnabled({ enabledWhen: 'offline' }, false), true);
assert.equal(ui.isControlEnabled({ enabledWhen: 'offline' }, true), false);
assert.equal(ui.isControlEnabled({ disabled: true }, true), false);

assert.deepEqual(
  ui.statusPresentation(
    {
      readiness: 'needs_setup',
      blockers: [{ message: 'Install the dedicated server binary.' }],
    },
    { state: 'not_installed' },
  ),
  {
    state: 'not_installed',
    label: 'SETUP NEEDED',
    tone: 'setup',
    reasons: ['Install the dedicated server binary.'],
  },
);
assert.deepEqual(
  ui.statusPresentation(
    { readiness: 'ready', blockers: [] },
    {
      state: 'running_degraded',
      process: { ok: true, running: true, error: null },
      listeners: [{ ok: false, error: 'ss unavailable' }],
      query: { attempted: true, ok: false, error: 'query timed out' },
    },
  ).reasons,
  ['ss unavailable', 'query timed out'],
);

const copy = ui.confirmationCopy({
  gameName: 'Minecraft Java',
  controlLabel: 'Maximum players',
  currentValue: 10,
  proposedValue: 18,
  risk: 'configuration',
  restartRequired: true,
});
assert.match(copy, /Minecraft Java/);
assert.match(copy, /10/);
assert.match(copy, /18/);
assert.match(copy, /restart/i);

console.log('ui tests passed');
