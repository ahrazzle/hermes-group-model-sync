// Dependency-free version-lockstep test (D11) — no npm packages.
//
// Run:  node test/plugin-version.test.js
// Exit code 0 = pass, 1 = fail.
//
// The ONE version that must never drift is the release number: plugin.yaml (the agent
// half the host parses), the desktop module (the half the app loads) and the dashboard
// manifest that publishes the pane's backend namespace must all agree, or a half-installed
// update ships an agent half and a desktop half from different releases.
//
// It also pins the interface facts that make the pane's read/write path work:
//   * desktop module `id` === dashboard/manifest.json `name` — ctx.rest('/presets') is
//     scoped to /api/plugins/<plugin id>, and the backend router is mounted under the
//     dashboard plugin's manifest name, so a mismatch silently 404s every route;
//   * manifest_version: 1 (the version the INSTALLER supports — never 2) and
//     api_version: 1 are declared as metadata, and python_dependencies is empty
//     (D13 — nothing may be lazily installed);
//   * config_schema is closed and EMPTY: v1 has one source of preset truth and the
//     unwired `preset_file` alternative-catalog key is not shipped (B-4);
//   * exactly one preset catalog ships, at presets/presets.json.
'use strict'
const assert = require('assert')
const fs = require('fs')
const path = require('path')

const ROOT = path.join(__dirname, '..')
let checks = 0
function ok(condition, message) {
  checks += 1
  assert.ok(condition, message)
}

const manifest = fs.readFileSync(path.join(ROOT, 'plugin.yaml'), 'utf8')
const desktop = fs.readFileSync(path.join(ROOT, 'desktop', 'plugin.js'), 'utf8')
const dashboard = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard', 'manifest.json'), 'utf8'))

function scalar(text, key) {
  const match = text.match(new RegExp(`^${key}:\\s*(.+)$`, 'm'))
  return match ? match[1].trim().replace(/^['"]|['"]$/g, '') : null
}

const yamlVersion = scalar(manifest, 'version')
const desktopVersion = (desktop.match(/^\s*version:\s*'([^']+)'/m) || [])[1]
const desktopId = (desktop.match(/^const ID = '([^']+)'/m) || [])[1]

ok(yamlVersion, 'plugin.yaml: version is required')
ok(desktopVersion, 'desktop/plugin.js: the default export must declare a version')
ok(yamlVersion === desktopVersion, `version drift: plugin.yaml ${yamlVersion} vs desktop ${desktopVersion}`)
ok(yamlVersion === dashboard.version, `version drift: plugin.yaml ${yamlVersion} vs dashboard/manifest.json ${dashboard.version}`)

ok(scalar(manifest, 'manifest_version') === '1', 'plugin.yaml: manifest_version must stay at the installer-supported 1')
ok(scalar(manifest, 'api_version') === '1', 'plugin.yaml: api_version must be 1')
ok(scalar(manifest, 'python_dependencies') === '[]', 'plugin.yaml: python_dependencies must be empty (D13)')

// Why manifest_version is 1 and not the "v2 fields" number: the LOADER accepts
// manifest_version 2 (hermes_cli/plugins_manifest.py:43) but the INSTALLER does not
// (hermes_cli/plugins_cmd.py:169 `_SUPPORTED_MANIFEST_VERSION = 1`, enforced at :597),
// so a v2 number makes the package un-installable via `hermes plugins install` on
// Hermes 0.21.3. The v2 FIELDS below it (api_version, config_schema,
// python_dependencies) are read regardless of this number — and a number the installer
// refuses is not "installable", however the loader behaves.

// B-4 — no unwired alternative-catalog key. `preset_file` was declared but never read:
// shipping a key nothing consumes is a false interface. v1 has exactly one catalog.
ok(!/preset_file/.test(manifest), 'plugin.yaml: the unwired config_schema.preset_file key must not be shipped')
// Hermes' config_schema is a mapping of setting name -> {type, default, description,
// required} (hermes_cli/plugins_manifest.py) — NOT a JSON Schema. An empty mapping is the
// honest "declares no settings" form; a JSON-Schema-shaped block would register a phantom
// setting named "properties" and log a warning per entry it cannot read.
ok(/^config_schema:\s*\{\s*\}\s*$/m.test(manifest), 'plugin.yaml: config_schema must be declared and empty (no settings ship)')
ok(!/^\s+additionalProperties:/m.test(manifest), 'plugin.yaml: config_schema is not JSON Schema — do not ship additionalProperties')
ok(!/^\s+properties:/m.test(manifest), 'plugin.yaml: config_schema is not JSON Schema — do not ship a properties block')
ok(!/^\s*(presets_file|preset_catalog|catalog):/m.test(manifest), 'plugin.yaml: no alternative preset-catalog key may be declared')

// The one catalog ships inside the package. `plugin.py` resolves it via its own
// presets_path() (= <install>/presets/presets.json) with no override parameter wired to
// any config value, so this file is the only catalog a shipped v1 can read.
const catalog = path.join(ROOT, 'presets', 'presets.json')
ok(fs.existsSync(catalog), 'presets/presets.json must ship (the one source of preset truth)')

function walk(dir) {
  const out = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === '.git' || entry.name === 'node_modules') continue
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...walk(full))
    else if (entry.name === 'presets.json') out.push(full)
  }
  return out
}
const catalogs = walk(ROOT)
ok(
  catalogs.length === 1 && catalogs[0] === catalog,
  `exactly one preset catalog may ship, at presets/presets.json — found ${JSON.stringify(catalogs)}`
)

ok(desktopId, 'desktop/plugin.js: the plugin id must be declared')
ok(
  desktopId === dashboard.name,
  `namespace mismatch: ctx.rest from desktop id ${JSON.stringify(desktopId)} reaches /api/plugins/${desktopId}, but the backend router mounts as ${JSON.stringify(dashboard.name)}`
)
ok(dashboard.api === 'plugin_api.py', 'dashboard/manifest.json must publish the backend router file')

console.log(`plugin-version.test.js: ${checks} checks passed (version ${yamlVersion}, id ${desktopId})`)
