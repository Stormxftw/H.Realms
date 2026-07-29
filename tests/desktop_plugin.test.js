const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const pluginPath = path.join(__dirname, '..', 'desktop-plugin', 'plugin.js');
const source = fs.readFileSync(pluginPath, 'utf8');

assert.match(source, /ROUTES_AREA/);
assert.match(source, /SIDEBAR_NAV_AREA/);
assert.match(source, /useQuery/);
assert.match(source, /useMutation/);
assert.match(source, /ConfirmDialog/);
assert.match(source, /ctx\.rest\(['"]\/proxy\/api\/status['"]/);
assert.match(source, /defaultEnabled:\s*false/);
assert.match(source, /const ROUTE = ['"]\/game-host['"]/);
assert.match(source, /disabledReason/);
assert.match(source, /blockers/);
assert.match(source, /running_degraded/);
assert.match(source, /unknown/);
assert.doesNotMatch(source, /<iframe|jsx\(['"]iframe['"]/);
assert.doesNotMatch(source, /https?:\/\//);

console.log('desktop plugin contract passed');
