// Dependency-free logic test for the group-member model READ path.
//
// Run:  node test/read-default.test.js
// Exit code 0 = pass, 1 = fail. Uses only Node's assert + fs (no npm deps).
//
// Covers the READ-path fix. Two independent bugs were fixed:
//
//  1. KEY BUG: the read used `config.get { key:'model' }`, which has NO
//     handler in the gateway and errors, so model/provider always came back
//     empty. The current model+provider are read via `config.get { key:'provider' }`,
//     which the gateway resolves through _resolve_model() and returns
//     `{ model, provider, providers }`. Reasoning still comes from
//     `config.get { key:'reasoning' }` -> `{ value, display }`.
//
//  2. SOURCE BUG: the Groups-tab MemberRow read through the resolved plumbing
//     session id. Group plumbing sessions are follow_profile_config: they
//     rebuild from the member PROFILE's current config on every resume, so the
//     authoritative value IS the profile default — `readMemberConfig(door, null)`
//     (config.get with NO session_id), the same source the durable
//     profiles.configure write targets. The resolved live sid is used ONLY as
//     a filter for the transient session.info overlay, never to read config.
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

// Extract a whole FUNCTION body even when its params are destructured
// (`function Fn({ a, b }) { ... }`) — `extract` above would stop at the first
// `}` of the param list. Starts at the body brace after the param list.
function extractFunction(opening) {
  const i = src.indexOf(opening)
  assert.notStrictEqual(i, -1, `extractFunction(): opening not found: ${opening}`)
  const bodyStart = src.indexOf(') {', i) + 2 // index OF the body '{'
  let depth = 0
  for (let j = bodyStart; j < src.length; j++) {
    if (src[j] === '{') depth++
    else if (src[j] === '}') {
      depth--
      if (depth === 0) return src.slice(i, j + 1)
    }
  }
  throw new Error(`extractFunction(): unbalanced: ${opening}`)
}

// Evaluate readMemberConfig in one shared scope with its two dependencies.
const probeCode =
  'const asRecord = o => (o && typeof o === \'object\' && !Array.isArray(o) ? o : {})\n' +
  extract('const rpcErrorText = err => {') +
  '\n' +
  extract('async function readMemberConfig')
// eslint-disable-next-line no-eval
eval(probeCode)
if (typeof readMemberConfig !== 'function') {
  const { runInThisContext } = require('vm')
  runInThisContext(probeCode)
}
globalThis.readMemberConfig = readMemberConfig

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
  // 1. sid=null (the Groups-tab authoritative read) must read model+provider
  //    via `config.get { key:'provider' }` and reasoning via `key:'reasoning'`,
  //    with NO session_id on either — i.e. the member PROFILE default, the
  //    same source the durable profiles.configure write targets.
  const door1 = makeDoor([
    { model: 'gpt-5', provider: 'openai' },
    { value: 'high' }
  ])
  const cfg1 = await readMemberConfig(door1, null)
  assert.strictEqual(door1.calls.length, 2, 'exactly two config.get calls')
  const providerCall = door1.calls.find(c => c.method === 'config.get' && c.params.key === 'provider')
  const reasonCall = door1.calls.find(c => c.method === 'config.get' && c.params.key === 'reasoning')
  assert.ok(providerCall && reasonCall, 'must read provider(model) AND reasoning')
  assert.ok(!door1.calls.some(c => c.params.key === 'model'), 'must NOT read via key:"model" (no gateway handler)')
  assert.deepStrictEqual(providerCall.params, { key: 'provider' }, 'profile-default provider read has NO session_id')
  assert.deepStrictEqual(reasonCall.params, { key: 'reasoning' }, 'profile-default reasoning read has NO session_id')
  assert.strictEqual(cfg1.model, 'gpt-5')
  assert.strictEqual(cfg1.provider, 'openai')
  assert.strictEqual(cfg1.reasoning, 'high')
  assert.strictEqual(cfg1.scope, 'default')

  // 2. sid given still works (session-scoped read): adds session_id to both
  //    keys and reflects a session-scoped scope. Not used on the member read
  //    path anymore, but readMemberConfig supports it for completeness.
  const door2 = makeDoor([
    { model: 'x', provider: 'y', scope: 'session' },
    { value: 'low' }
  ])
  const cfg2 = await readMemberConfig(door2, 'sess-1')
  assert.deepStrictEqual(door2.calls[0].params, { key: 'provider', session_id: 'sess-1' })
  assert.strictEqual(cfg2.model, 'x')
  assert.strictEqual(cfg2.scope, 'session')

  // 3. MemberRow (Groups tab) source: the authoritative load read must be the
  //    member PROFILE default — readMemberConfig(door, null) — and the buggy
  //    session-scoped read `readMemberConfig(door, live.sid)` must be gone.
  const memberRow = extractFunction('function MemberRow({ member, row, room, group, refreshEpoch })')
  assert.ok(
    memberRow.includes('readMemberConfig(door, null)'),
    'MemberRow load must read the member PROFILE default (sid null)'
  )
  assert.ok(
    !memberRow.includes('readMemberConfig(door, live.sid)'),
    'MemberRow must NOT read config through a resolved plumbing session id'
  )
  // resolveLiveSessionId is still used — but only to learn liveSid for the
  // transient session.info overlay filter, never as a config read handle.
  assert.ok(memberRow.includes('resolveLiveSessionId'), 'live sid resolution still feeds the session.info overlay')

  // 4. AgentRow (Agents tab) source: also reads the agent profile default.
  const agentRow = extractFunction('function AgentRow({ row, refreshEpoch })')
  assert.ok(agentRow.includes('readMemberConfig(door, null)'), 'AgentRow must read the agent profile default')

  console.log('read-default.test.js: 4/4 passed')
}

main().catch(err => {
  console.error('FAIL:', err.message)
  process.exit(1)
})
