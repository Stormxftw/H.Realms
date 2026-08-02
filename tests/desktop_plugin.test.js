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
// Operation polling is extracted to behavior.mjs and wired through a signal.
assert.match(source, /waitForOperation\(path => ctx\.rest\(path, \{ timeoutMs: 15_000 \}\), queued, \{ signal: runtime\.signal \}\)/);
assert.match(source, /from '\.\/behavior\.mjs'/);
assert.match(source, /ctx\.onDispose\(\(\) => controller\.abort\(\)\)/);
assert.match(source, /ctx\.i18n\.register\(\{[\s\S]*gameHost:[\s\S]*openLabel:/);
assert.match(source, /usePluginI18n\(ID\)/);
assert.match(source, /defaultEnabled:\s*false/);
assert.match(source, /const ROUTE = ['"]\/game-host['"]/);
assert.match(source, /disabledReason/);
assert.match(source, /blockers/);
assert.match(source, /running_degraded/);
assert.match(source, /unknown/);
assert.doesNotMatch(source, /<iframe|jsx\(['"]iframe['"]/);
assert.doesNotMatch(source, /https?:\/\//);

console.log('desktop plugin contract passed');