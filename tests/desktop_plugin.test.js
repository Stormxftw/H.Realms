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
// Desktop plugins are raw, single-file ESM. Only app-provided modules resolve.
const importedModules = [...source.matchAll(/from\s+['"]([^'"]+)['"]/g)].map(match => match[1]);
assert.deepEqual(importedModules, ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime']);
assert.doesNotMatch(source, /from\s+['"]\.\.?\//);
assert.match(source, /function waitForOperation\(/);
assert.match(source, /function selectKey\(/);
assert.match(source, /function numericDraft\(/);
assert.match(source, /waitForOperation\([\s\S]*ctx\.rest\(path, \{ timeoutMs: 15_000 \}\)[\s\S]*signal: runtime\.signal/);
assert.match(source, /artifactId/);
assert.match(source, /requiredConfirmation/);
assert.match(source, /\/proxy\/api\/backups\/\$\{gameId\}\/restore\/preview/);
assert.match(source, /\/proxy\/api\/diagnostics\/\$\{selectedGameId\}\/logs/);
assert.match(source, /\/proxy\/api\/diagnostics\/\$\{game\.id\}\/logs\/\$\{encodeURIComponent\(logId\)\}/);
assert.match(source, /async function handleLogTail\(logId\)[\s\S]*setLogTail\(\{ logId, state: ['"]loading['"], content: ['"]['"] \}\)[\s\S]*await ctx\.rest/);
assert.match(source, /logTail\?\.logId === logId/);
assert.match(source, /onClose: \(\) => setLogTail\(null\)/);
assert.match(source, /ctx\.rest\(`\/art\/\$\{selectedGameId\}`\)/);
assert.match(source, /artQuery\.data\?\.dataUrl/);
assert.match(source, /objectPosition: artQuery\.data\?\.objectPosition/);
assert.match(source, /alt: ''/);
assert.doesNotMatch(source, /window\.confirm\(/);
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