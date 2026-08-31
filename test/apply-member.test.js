// Dependency-free logic test for the group-member durable model write.
//
// Run:  node test/apply-member.test.js
// Exit code 0 = pass, 1 = fail. Uses only Node's assert + fs (no npm deps).
//
// Covers the ROOT-CAUSE FIX: a group member's model CANNOT be changed by
// `config.set --session` on the member's plumbing sid. Group member plumbing
// sessions are follow_profile_config: they rebuild from the member PROFILE's
// current config on every resume and discard session-scoped pins (the backend
// `_stored_session_runtime_overrides` returns {} for them), and the sid is not
// resident in the gateway between turns anyway, so a session-scoped write
// silently no-ops. The durable write must go through `profiles.configure`
// { name, model, provider } on the member's door — the same RPC the built-in
// Bots editor uses — which preserves the confirm_expensive_model round-trip.
'use strict'
const assert = require('assert')
const fs = require('fs')
const path = require('path')

const PLUGIN = path.join(__dirname, '..', 'desktop', 'plugin.js')
const src = fs.readFileSync(PLUGIN, 'utf8')

// Extract a top-level function/const by its opening line, balanced-brace aware.
function extract(opening) {
  const i = src.indexOf(opening)
  assert.notStrictEqual(i, -1, `extract(): opening not found: ${opening}`)
  let depth = 0
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++
    else if (src[j] === '}') {
      depth--
      if (depth === 0) return src.slice(i, j + 1)
    }
  }
  throw new Error(`extract(): unbalanced: ${opening}`)
}

// Evaluate memberProfileName + applyMemberConfig in one shared scope.
const asRecord = o => (o && typeof o === 'object' && !Array.isArray(o) ? o : {})
const probeCode =
  extract('const memberProfileName = member => {') +
  '\n' +
  extract('async function applyMemberConfig') +
  '\n'
// eslint-disable-next-line no-eval
eval(probeCode)
if (typeof applyMemberConfig !== 'function') {
  const { runInThisContext } = require('vm')
  runInThisContext(probeCode)
}
globalThis.asRecord = asRecord
globalThis.memberProfileName = memberProfileName
globalThis.applyMemberConfig = applyMemberConfig

const member = { name: 'azaraki' }
const cfg = { model: 'gpt-5', provider: 'openai', reasoning: 'high' }

// A recording door: captures every request so the test can assert the exact
// call-shape, and returns a programmable per-call response.
function makeDoor(responses = []) {
  const calls = []
  let i = 0
  return {
    calls,
    request: async (method, params) => {
      calls.push({ method, params })
      const r = responses[i]
      i += 1
      if (r instanceof Error) throw r
      return r
    }
  }
}

async function main() {
  // 1. The model write must be `profiles.configure` on the member's door with
  //    the member PROFILE name — NOT config.set, NOT a session-scoped --session
  //    value, and NO session_id (this is the whole point of the fix).
  const door1 = makeDoor([{ applied: { model: true }, ok: true }])
  const r1 = await applyMemberConfig(door1, member, cfg)
  assert.strictEqual(r1.needsConfirm, false)
  const modelCall = door1.calls.find(c => c.method === 'profiles.configure')
  assert.ok(modelCall, 'must issue profiles.configure for the model write')
  assert.deepStrictEqual(
    { name: modelCall.params.name, model: modelCall.params.model, provider: modelCall.params.provider },
    { name: 'azaraki', model: 'gpt-5', provider: 'openai' },
    'profiles.configure params must carry the member profile name + model + provider'
  )
  assert.strictEqual(modelCall.params.session_id, undefined, 'no session_id on the durable profile write')
  assert.strictEqual(modelCall.params.confirm_expensive_model, undefined, 'no confirm flag on the unconfirmed first call')

  // Reasoning follows the same profile-default semantics via config.set scope:global.
  const reasoningCall = door1.calls.find(c => c.method === 'config.set')
  assert.ok(reasoningCall, 'must issue config.set for reasoning')
  assert.deepStrictEqual(reasoningCall.params, { key: 'reasoning', value: 'high', scope: 'global' })

  // 2. confirm_required round-trip: unconfirmed guarded pick → needsConfirm,
  //    nothing written; resend with confirm_expensive_model:true → applied.
  const door2 = makeDoor([
    { ok: false, applied: {}, confirm_required: true, confirm_message: 'pricey model' },
    { ok: true, applied: { model: true } }
  ])
  const pending = await applyMemberConfig(door2, member, cfg)
  assert.strictEqual(pending.needsConfirm, true)
  assert.strictEqual(pending.message, 'pricey model')
  // First call must NOT have re-sent the flag.
  assert.strictEqual(door2.calls[0].params.confirm_expensive_model, undefined)
  // Second call re-sends the SAME profiles.configure shape WITH the flag.
  const confirmed = await applyMemberConfig(door2, member, cfg, true)
  assert.strictEqual(confirmed.needsConfirm, false)
  const second = door2.calls[1]
  assert.strictEqual(second.method, 'profiles.configure')
  assert.deepStrictEqual(second.params, {
    name: 'azaraki',
    model: 'gpt-5',
    provider: 'openai',
    confirm_expensive_model: true
  })

  // 3. A member without a profile name, or missing model/provider, is a no-op
  //    (never fires a request) — guards the crash path.
  const door3 = makeDoor([])
  await applyMemberConfig(door3, { profile: '' }, { model: 'x', provider: 'y' })
  assert.strictEqual(door3.calls.length, 0, 'no profile name → no request')
  const door4 = makeDoor([])
  await applyMemberConfig(door4, member, { model: '', provider: '' })
  assert.strictEqual(door4.calls.length, 0, 'missing model/provider → no request')

  console.log('apply-member.test.js: 4/4 passed')
}

main().catch(err => {
  console.error('FAIL:', err.message)
  process.exit(1)
})
