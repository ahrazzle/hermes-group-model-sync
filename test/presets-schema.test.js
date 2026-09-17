// Dependency-free schema test for presets/presets.json — the single source of preset
// truth (D4/D10). No npm packages: Node's assert + fs only.
//
// Run:  node test/presets-schema.test.js
// Exit code 0 = pass, 1 = fail.
//
// What it pins:
//   * the file parses and declares the supported schema version;
//   * every preset: id shape + uniqueness, non-empty name, boolean guarded;
//   * tiers.main present and naming provider and/or model — or null ONLY when the
//     preset carries per-profile `assignments` (each assigned tier validated the same
//     way; mirrors plugin.py::_validate_preset);
//   * reasoning_effort, when named, is one of the seven live values;
//   * base_url/api_mode are null ("not named") or a non-empty string — never "" (I2:
//     absent != null != "clear it");
//   * NO unknown keys anywhere (a typo'd key would otherwise be silently inert);
//   * the shipped catalog carries at least two presets, including a minimal one that
//     names no fallback and no reasoning (so the "only named keys" rule has a real
//     example attached to it);
//   * a preset carrying machine-specific values is labelled EXAMPLE ONLY in its name
//     and description (O4 — an illustrative carry, never a fleet default or a
//     recommendation).
'use strict'
const assert = require('assert')
const fs = require('fs')
const path = require('path')

const PRESETS = path.join(__dirname, '..', 'presets', 'presets.json')

const REASONING = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
const PRESET_KEYS = ['id', 'name', 'description', 'tiers', 'guarded', 'assignments']
const TIER_KEYS = ['main', 'aux', 'fallback']
const TIER_MODEL_KEYS = ['provider', 'model', 'base_url', 'api_mode', 'reasoning_effort']
const FALLBACK_ENTRY_KEYS = ['provider', 'model']

let checks = 0

function ok(condition, message) {
  checks += 1
  assert.ok(condition, message)
}

function unknown(mapping, allowed, where) {
  const extra = Object.keys(mapping).filter(k => !allowed.includes(k)).sort()
  ok(extra.length === 0, `${where}: unknown key(s) ${extra.join(', ')}`)
}

function checkTierModel(tier, where) {
  unknown(tier, TIER_MODEL_KEYS, where)
  ok('provider' in tier || 'model' in tier, `${where}: must name provider and/or model`)
  for (const key of ['provider', 'model']) {
    if (key in tier && tier[key] !== null) {
      ok(typeof tier[key] === 'string' && tier[key].trim() !== '', `${where}.${key}: non-empty string`)
    }
  }
  for (const key of ['base_url', 'api_mode']) {
    if (key in tier) {
      const value = tier[key]
      ok(
        value === null || (typeof value === 'string' && value.trim() !== ''),
        `${where}.${key}: null ("not named") or a non-empty string — "" is not a value`
      )
    }
  }
  if ('reasoning_effort' in tier && tier.reasoning_effort !== null) {
    ok(
      REASONING.includes(tier.reasoning_effort),
      `${where}.reasoning_effort: ${tier.reasoning_effort} must be one of ${REASONING.join('|')}`
    )
  }
}

const raw = fs.readFileSync(PRESETS, 'utf8')
const data = JSON.parse(raw)

unknown(data, ['schema', 'presets'], 'presets.json')
ok(data.schema === 1, `presets.json: schema must be 1, got ${JSON.stringify(data.schema)}`)
ok(Array.isArray(data.presets) && data.presets.length > 0, 'presets.json: presets must be a non-empty list')
ok(data.presets.length >= 2, 'presets.json: ship at least two presets (one fleet-default-shaped, one minimal)')

const ids = new Set()
let minimalCount = 0
for (const preset of data.presets) {
  unknown(preset, PRESET_KEYS, `preset ${preset.id || '(no id)'}`)
  ok(/^[a-z0-9][a-z0-9-]{0,63}$/.test(String(preset.id || '')), `preset id ${JSON.stringify(preset.id)} must match [a-z0-9][a-z0-9-]*`)
  ok(!ids.has(preset.id), `preset id ${preset.id} is duplicated`)
  ids.add(preset.id)
  ok(typeof preset.name === 'string' && preset.name.trim() !== '', `preset ${preset.id}: name required`)
  ok(typeof preset.description === 'string', `preset ${preset.id}: description must be a string`)
  ok(typeof preset.guarded === 'boolean', `preset ${preset.id}: guarded must be a boolean`)

  const tiers = preset.tiers
  ok(tiers && typeof tiers === 'object' && !Array.isArray(tiers), `preset ${preset.id}: tiers required`)
  unknown(tiers, TIER_KEYS, `preset ${preset.id}.tiers`)
  // Per-profile assignments (additive, optional) — the Python core mirrors every rule
  // here (plugin.py::_validate_preset). When assignments is present, tiers.main may be
  // null; an unassigned target is then REFUSED by plan/apply, never routed by guess.
  let assignments = null
  if (preset.assignments !== undefined && preset.assignments !== null) {
    ok(typeof preset.assignments === 'object' && !Array.isArray(preset.assignments) &&
       Object.keys(preset.assignments).length > 0,
       `preset ${preset.id}: assignments must be a non-empty mapping of profile -> tier`)
    assignments = preset.assignments
    for (const [profile, tier] of Object.entries(assignments)) {
      ok(typeof profile === 'string' && profile.trim() !== '', `preset ${preset.id}: assignments profile key must be non-empty`)
      ok(tier && typeof tier === 'object' && !Array.isArray(tier), `preset ${preset.id}.assignments[${profile}]: must be a mapping`)
      checkTierModel(tier, `preset ${preset.id}.assignments[${profile}]`)
    }
  }
  if (tiers.main === null || tiers.main === undefined) {
    ok(assignments !== null, `preset ${preset.id}: tiers.main is null but no per-profile assignments name a route — refusing to ship an inert preset`)
  } else {
    ok(typeof tiers.main === 'object' && !Array.isArray(tiers.main), `preset ${preset.id}: tiers.main must be a mapping or null`)
    checkTierModel(tiers.main, `preset ${preset.id}.tiers.main`)
  }
  if (tiers.aux !== undefined && tiers.aux !== null) {
    checkTierModel(tiers.aux, `preset ${preset.id}.tiers.aux`)
  }
  if (tiers.fallback !== undefined && tiers.fallback !== null) {
    ok(Array.isArray(tiers.fallback) && tiers.fallback.length > 0, `preset ${preset.id}: tiers.fallback must be a non-empty list or null`)
    for (const [i, entry] of tiers.fallback.entries()) {
      unknown(entry, FALLBACK_ENTRY_KEYS, `preset ${preset.id}.tiers.fallback[${i}]`)
      ok(typeof entry.provider === 'string' && entry.provider.trim() !== '', `fallback[${i}].provider required`)
      ok(typeof entry.model === 'string' && entry.model.trim() !== '', `fallback[${i}].model required`)
    }
  }
  // A "minimal" preset names no reasoning and no fallback — the example the
  // absent-key rule is demonstrated against.
  const mainObj = tiers.main || {}
  const named = ['provider', 'model', 'base_url', 'api_mode', 'reasoning_effort']
    .filter(k => mainObj[k] !== undefined && mainObj[k] !== null)
  if (!named.includes('reasoning_effort') && !tiers.fallback) {
    minimalCount += 1
  }
}

ok(minimalCount >= 1, 'presets.json: at least one preset must be minimal (no reasoning_effort, no fallback)')

// O4 — a preset carrying machine-specific values in its GLOBAL main tier must be VISIBLY
// an example: these are illustrative carries, not fleet defaults and not a recommendation.
// "Machine-valued" means it names a concrete provider/model/base_url. A per-profile
// assignments preset is NOT an example — its values are a named fleet array with a stated
// source, and it carries no global main tier, so it is checked for provenance instead.
let machineValued = 0
let assignmentValued = 0
for (const preset of data.presets) {
  const main = preset.tiers.main || {}
  const named = ['provider', 'model', 'base_url'].filter(
    k => typeof main[k] === 'string' && main[k].trim() !== ''
  )
  if (named.length > 0) {
    machineValued += 1
    ok(
      /\(example\)/i.test(preset.name),
      `preset ${preset.id}: a machine-valued preset must be labelled "(example)" in its name — got ${JSON.stringify(preset.name)}`
    )
    ok(
      preset.description.includes('EXAMPLE ONLY'),
      `preset ${preset.id}: a machine-valued preset's description must say "EXAMPLE ONLY" — got ${JSON.stringify(preset.description)}`
    )
    continue
  }
  if (preset.assignments) {
    assignmentValued += 1
    ok(
      !/\(example\)/i.test(preset.name) && !preset.description.includes('EXAMPLE ONLY'),
      `preset ${preset.id}: an assignments preset carries a named real array, not an example — drop the example labelling`
    )
    ok(
      /\d{4}-\d{2}-\d{2}/.test(preset.description),
      `preset ${preset.id}: an assignments preset must state the date its array was read (provenance)`
    )
  }
}
ok(machineValued >= 2, 'presets.json: the shipped examples must carry real values (so plan has something to diff)')

console.log(`presets-schema.test.js: ${checks} checks passed — presets: ${[...ids].join(', ')}`)
