// Dependency-free node test for the preset DIFF rules — no npm packages, but it drives
// the ONE implementation (`plugin.py --diff`, the same `diff_preset` the CLI verbs and
// the desktop pane's /presets/plan route call) rather than a second, drifting copy of
// the rule in JavaScript.
//
// Run:  node test/preset-diff.test.js
// Exit code 0 = pass, 1 = fail.
//
// Prerequisite: `python3` with PyYAML — the repo ships a Python agent half, so this is
// the same interpreter the plugin itself runs on. Override with PYTHON=/path/to/python.
//
// What it pins (design spec §4):
//   I2  a key the preset does not name (absent, or null) is never a row, never written;
//   I3  a diff against an already-applied config is EMPTY (the idempotency rule);
//   minimality: only keys whose value actually differs appear in `changes`;
//   scope: every key reported is one of the six locked target keys.
'use strict'
const assert = require('assert')
const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const ROOT = path.join(__dirname, '..')
const PY = process.env.PYTHON || 'python3'
const SCOPED_KEYS = [
  'model.provider',
  'model.default',
  'model.base_url',
  'model.api_mode',
  'agent.reasoning_effort',
  'fallback_providers'
]

let checks = 0
function ok(condition, message) {
  checks += 1
  assert.ok(condition, message)
}

function runDiff(current, presetId, presetsFile) {
  const args = [path.join(ROOT, 'plugin.py'), 'diff', '--preset', presetId]
  if (presetsFile) {
    args.push('--presets', presetsFile)
  }
  return spawnSync(PY, args, {
    cwd: ROOT,
    input: JSON.stringify(current),
    encoding: 'utf8'
  })
}

function diff(current, presetId, presetsFile) {
  const res = runDiff(current, presetId, presetsFile)
  if (res.status !== 0) {
    throw new Error(`plugin.py diff --preset ${presetId} failed (${res.status}): ${res.stderr || res.stdout}`)
  }
  return JSON.parse(res.stdout)
}

function keysOf(result) {
  return [...result.changes.map(c => c.key), ...result.unchanged.map(u => u.key)]
}

function applyTo(current, result) {
  const next = JSON.parse(JSON.stringify(current))
  for (const change of result.changes) {
    const segments = change.key.split('.')
    let cursor = next
    for (const segment of segments.slice(0, -1)) {
      if (!cursor[segment] || typeof cursor[segment] !== 'object') {
        cursor[segment] = {}
      }
      cursor = cursor[segment]
    }
    cursor[segments[segments.length - 1]] = change.after
  }
  return next
}

// ── the probe ──────────────────────────────────────────────────────────────
const probe = spawnSync(PY, ['-c', 'import yaml'], { encoding: 'utf8' })
if (probe.status !== 0) {
  console.error(`FAIL: "${PY}" with PyYAML is required to run the diff engine (set PYTHON=...)`)
  process.exit(1)
}

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'gms-diff-'))
const fixtureCatalog = path.join(tmp, 'presets.json')
fs.writeFileSync(
  fixtureCatalog,
  JSON.stringify({
    schema: 1,
    presets: [
      {
        id: 'route-only',
        name: 'Route only (fixture)',
        description: 'names provider + model only; everything else is null on purpose',
        guarded: false,
        tiers: {
          main: { provider: 'prov-a', model: 'model-a', base_url: null, api_mode: null, reasoning_effort: null },
          aux: null,
          fallback: null
        }
      }
    ]
  })
)

const current = {
  model: { provider: 'prov-b', default: 'model-a', base_url: 'https://old.example/v1', api_mode: 'chat_completions' },
  agent: { reasoning_effort: 'low' },
  fallback_providers: [{ provider: 'nous', model: 'keep-me' }]
}

// 1. I2 + minimality — only the keys the preset NAMES, and only the ones that differ.
const routeOnly = diff(current, 'route-only', fixtureCatalog)
ok(routeOnly.changes.length === 1, `route-only: expected exactly 1 change (provider), got ${routeOnly.changes.length}: ${JSON.stringify(routeOnly.changes)}`)
ok(routeOnly.changes.some(c => c.key === 'model.provider' && c.before === 'prov-b' && c.after === 'prov-a'), 'model.provider must change prov-b -> prov-a')
ok(routeOnly.unchanged.some(u => u.key === 'model.default' && u.value === 'model-a'), 'model.default already matches -> reported unchanged, not rewritten')
const namedKeys = keysOf(routeOnly)
ok(!namedKeys.includes('model.base_url'), 'model.base_url is null in the preset -> must not appear at all')
ok(!namedKeys.includes('model.api_mode'), 'model.api_mode is null in the preset -> must not appear at all')
ok(!namedKeys.includes('agent.reasoning_effort'), 'agent.reasoning_effort not named -> must not appear at all')
ok(!namedKeys.includes('fallback_providers'), 'fallback_providers not named -> must not appear at all')

// 2. scope — nothing outside the six locked keys, ever.
for (const key of diff(current, 'minimal').changes.concat(diff(current, 'minimal').unchanged).map(r => r.key)) {
  ok(SCOPED_KEYS.includes(key), `${key} is outside the locked target key scope`)
}

// 3. I3 — a diff against the already-applied config is empty.
const applied = applyTo(current, diff(current, 'minimal'))
const second = diff(applied, 'minimal')
ok(second.changes.length === 0, `idempotency: second diff must have no changes, got ${JSON.stringify(second.changes)}`)
ok(second.unchanged.length > 0, 'idempotency: the named keys must be reported unchanged')
ok(keysOf(second).every(k => SCOPED_KEYS.includes(k)), 'idempotency: only locked keys reported')

// 4. absent vs null vs "clear it" — a key present in the target but unnamed by the
//    preset keeps its value. 'route-only' names provider + model only, so base_url,
//    api_mode, reasoning and fallback must all survive an apply of it.
const routeApplied = applyTo(current, routeOnly)
ok(routeApplied.model.base_url === 'https://old.example/v1', 'unnamed model.base_url must survive')
ok(routeApplied.model.api_mode === 'chat_completions', 'unnamed model.api_mode must survive')
ok(routeApplied.fallback_providers.length === 1 && routeApplied.fallback_providers[0].model === 'keep-me', 'unnamed fallback_providers must survive')
ok(routeApplied.agent.reasoning_effort === 'low', 'unnamed agent.reasoning_effort must survive')
ok(routeApplied.model.provider === 'prov-a', 'the named provider is written')

// the minimal preset DOES name the whole route, so its base_url lands as named.
ok(applied.model.provider === 'deepseek' && applied.model.base_url === 'https://api.deepseek.com/v1', 'the minimal preset writes its named route')
ok(applied.agent.reasoning_effort === 'low' && applied.fallback_providers[0].model === 'keep-me', 'the minimal preset leaves reasoning and fallback alone')

// 5. fail closed: an unknown preset id, and a catalog with an unknown key.
ok(runDiff(current, 'no-such-preset').status !== 0, 'unknown preset id must exit non-zero')
const badCatalog = path.join(tmp, 'bad.json')
fs.writeFileSync(badCatalog, JSON.stringify({ schema: 1, presets: [{ id: 'x', name: 'x', description: '', guarded: false, typos: {}, tiers: { main: { provider: 'a' }, aux: null, fallback: null } }] }))
ok(runDiff(current, 'x', badCatalog).status !== 0, 'a preset with an unknown key must be refused, not silently ignored')

fs.rmSync(tmp, { recursive: true, force: true })
console.log(`preset-diff.test.js: ${checks} checks passed (engine: ${PY} plugin.py diff)`)
