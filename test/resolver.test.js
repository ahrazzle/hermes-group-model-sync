// Dependency-free logic test for the group-model-sync live-session resolver.
//
// Run:  node test/resolver.test.js
// Exit code 0 = pass, 1 = fail. Uses only Node's assert + fs (no npm deps).
//
// Covers the bug this fix removes: the persisted Bot Mode room map
// (hermes.plugin.hermes-bots.group-chats → sessions[memberKey]) can hold a
// ws-orphan-reaped sid, so config.set was silently no-op'ing against a dead
// session. resolveLiveSessionId must prefer the CURRENT live session from the
// backend and fall back to the persisted map only when no live session exists.
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

// Evaluate memberKey + resolveLiveSessionId in one shared scope. Function
// declarations hoist; `const memberKey` is block-scoped and won't attach to a
// plain eval context, so we explicitly publish both to globalThis after eval.
const asRecord = o => (o && typeof o === 'object' && !Array.isArray(o) ? o : {})
const probeCode = extract('const memberKey = member => {') + '\n' + extract('async function resolveLiveSessionId') + '\n'
// eslint-disable-next-line no-eval
eval(probeCode)
if (typeof resolveLiveSessionId !== 'function') {
  // strict-mode eval keeps declarations local — fall back to an indirect eval
  // into global scope so the test can reach them.
  const { runInThisContext } = require('vm')
  runInThisContext(probeCode)
}
globalThis.asRecord = asRecord
globalThis.memberKey = memberKey
globalThis.resolveLiveSessionId = resolveLiveSessionId

const STALE_SID = '20260819_023417_a5fbb0' // persisted map → reaped Aug-29
const LIVE_SID = '20260818_195823_d9de95' // current live Group: Nani session
const room = { roomId: null, sessions: { azaraki: STALE_SID } }
const member = { name: 'azaraki' }

// 1. Live session list contains the real Group: Nani session → must win over
//    the stale persisted sid (this is the exact bug).
const liveDoor = {
  request: async () => ({
    sessions: [
      { id: LIVE_SID, title: 'Group: Nani', resolved_id: LIVE_SID },
      { id: 'other', title: 'Group: Tafsir', resolved_id: 'other' },
    ],
  }),
}

// 2. Desktop closed / all reaped → live list empty → graceful persisted fallback.
const deadDoor = { request: async () => ({ sessions: [] }) }

// 3. Compression tip: resolved_id differs from id → the tip is the live target.
const tipDoor = {
  request: async () => ({
    sessions: [{ id: 'old-root', title: 'Group: Nani', resolved_id: LIVE_SID }],
  }),
}

// 4. Backend error → must not throw; falls back to persisted map.
const errDoor = { request: async () => { throw new Error('boom') } }

async function main() {
  const t1 = await resolveLiveSessionId(liveDoor, member, 'Nani', room)
  assert.deepStrictEqual(t1, { sid: LIVE_SID, live: true },
    'live match must beat stale persisted sid')

  const t2 = await resolveLiveSessionId(deadDoor, member, 'Nani', room)
  assert.deepStrictEqual(t2, { sid: STALE_SID, live: false },
    'no live session → graceful persisted fallback')

  const t3 = await resolveLiveSessionId(tipDoor, member, 'Nani', room)
  assert.strictEqual(t3.sid, LIVE_SID, 'resolved_id (compression tip) must be used')

  const t4 = await resolveLiveSessionId(errDoor, member, 'Nani', room)
  assert.strictEqual(t4.live, false, 'backend error → non-live fallback, no throw')

  console.log('resolver.test.js: 4/4 passed')
}

main().catch(err => {
  console.error('FAIL:', err.message)
  process.exit(1)
})
