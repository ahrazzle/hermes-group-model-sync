/**
 * group-model-sync — Group Model Selector + Agent-wide model sync for Hermes Desktop.
 *
 * Two features, one pane:
 *
 *   Groups tab — pick a group chat (Bot Mode room) and see every member
 *   agent's current model + reasoning level, then change either per member.
 *   Up to 6 members per room. The same provider/model dropdowns the app's
 *   single-session picker uses, duplicated per member. The durable per-member
 *   write goes through the Bots editor's own RPC (profiles.configure with the
 *   member profile name) — NOT config.set --session on a plumbing sid, because
 *   group member plumbing sessions are follow_profile_config: they rebuild
 *   from the member PROFILE's current config on every resume and discard any
 *   session-scoped pin (see applyMemberConfig).
 *
 *   Agents tab — pick an agent profile, set model + reasoning, and press
 *   Sync to push that configuration to EVERY existing session of that agent
 *   (canonical Bot Chat, group plumbing sessions, cron sessions, anything
 *   else) — exactly as if the user had opened each session and picked the
 *   model manually.
 *
 * Data sources (all local, no telemetry):
 *   - Roster: `profiles.list` on the active gateway (the same rich rows the
 *     Bots pane renders: name, display_name, ui_meta, canonical_session),
 *     plus host.agents() union rows for other connections.
 *   - Bot Mode room map: localStorage key `hermes.plugin.hermes-bots.group-chats`
 *     (the bundled hermes-bots plugin's own persisted room records — read-only,
 *     degrade gracefully if the format changes).
 *   - Gateway RPCs routed to each agent's own source via host.requestProfile,
 *     or the active gateway via host.request when a row has no route.
 *
 * No upstream approval needed: this is a standalone desktop plugin.
 */

import {
  Badge,
  Button,
  Codicon,
  EmptyState,
  GlyphSpinner,
  host,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  surfaceModelSwitchConfirm,
  Tip
} from '@hermes/plugin-sdk'
import { jsx } from 'react/jsx-runtime'
import { useEffect, useMemo, useState } from 'react'

const ID = 'group-model-sync'
const BOTS_ROOMS_KEY = 'hermes.plugin.hermes-bots.group-chats'
const MAX_GROUP_MEMBERS = 6
const REASONING_LEVELS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
const REASONING_LABELS = {
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'X-High',
  max: 'Max',
  ultra: 'Ultra'
}
const NONE = '__none__'

// ── tiny helpers ────────────────────────────────────────────────────────────

const asRecord = value => (value && typeof value === 'object' && !Array.isArray(value) ? value : {})

/** A roster row's identity. profiles.list rows carry `name`; host.agents()
 *  union rows carry `profile` (and `handle`) but NOT `name`. */
const rowName = row => {
  const r = asRecord(row)
  return String(r.name || r.profile || r.handle || '').trim()
}

const displayNameOf = row => {
  const r = asRecord(row)
  return String(r.display_name || r.title || r.name || r.profile || 'agent')
}

/** Stable per-member identity inside a Bot Mode room (mirrors hermes-bots
 *  groupMemberKey): bare name for local members, connection-qualified for
 *  source-scoped/remote members. */
const memberKey = member => {
  const m = asRecord(member)
  return m.sourceScoped || m.remoteSource
    ? `${m.connectionId || 'legacy'}::${m.name || m.profile || 'default'}`
    : m.name || m.profile || 'default'
}

/** The member's AGENT PROFILE name — the durable target for a group member's
 *  model write. Group member plumbing sessions are ``follow_profile_config``:
 *  they rebuild from the member PROFILE's current config on every resume, so
 *  the per-member model switch must land on the profile default, not on a
 *  session-scoped pin. */
const memberProfileName = member => {
  const m = asRecord(member)
  return String(m.name || m.profile || '').trim()
}

/** Resolve a member's CURRENT live session id for a group at apply time.
 *
 * The persisted Bot Mode room map (`hermes.plugin.hermes-bots.group-chats`,
 * field ``sessions[memberKey]``) can go STALE: when a member's plumbing
 * session is reaped as a ws-orphan (connection dropped, no clean close — which
 * happens on every desktop close), the desktop reconnects and mints a NEW
 * session, but the durable map keeps pointing at the reaped one until the next
 * ``ensureGroupChatSession`` turn. ``config.set`` with that dead sid falls into
 * the gateway's non-live else branch and silently no-ops — pins never change,
 * no tokens ever move to the target model. That is exactly the class of bug
 * this resolver removes.
 *
 * Resolution ladder (mirrors the bundled hermes-bots plugin's own live-session
 * lookup, robust to the reap-on-close behaviour):
 *   1. ``session.list`` (live list, ``include_hidden``) and match the room's
 *      plumbing title ``Group: ${room.roomId || group}``. The live list is
 *      ``ended_at IS NULL``-filtered, so a ws-orphan-reaped session NEVER
 *      matches — only the current live session does. (A raw title-lookup would
 *      not be enough: it returns ``[]`` for an archived row, so after a reap it
 *      would hide the live session and force a stale fallback.)
 *   2. Fall back to the persisted map sid only when the live list contains no
 *      matching title (fresh room, or a backend that predates the title).
 * Returns ``{ sid, live }``.
 */
async function resolveLiveSessionId(door, member, group, room) {
  const title = `Group: ${room?.roomId || group}`
  const key = memberKey(member)
  try {
    const res = await door.request('session.list', { include_hidden: true, limit: 500 })
    const sessions = Array.isArray(res?.sessions) ? res.sessions : []
    const hit = sessions.find(s => s && (s.title || '') === title && (s.resolved_id || s.id))
    if (hit) {
      return { sid: hit.resolved_id || hit.id, live: true }
    }
  } catch {
    /* live lookup failed — fall through to the persisted sid below */
  }
  const known = room?.sessions?.[key]
  return { sid: typeof known === 'string' ? known : null, live: false }
}

/** Route descriptor for an agent row; null when the row rides the active
 *  gateway. Mirrors Bot Mode's resolveBotConnectionRoute. */
const routeFor = row => {
  const r = asRecord(row)
  const candidate = asRecord(r.route)
  const connectionId = String(candidate.connectionId || r.connectionId || '').trim()
  if (!connectionId) {
    return null
  }
  const profile = String(candidate.profile || r.profile || r.name || '').trim() || 'default'
  const targetProfile = String(candidate.targetProfile || r.targetProfile || profile).trim() || profile
  return {
    connectionId,
    mode: candidate.mode === 'local' || r.connectionKind === 'local' || connectionId === 'local' ? 'local' : 'remote',
    profile,
    targetProfile
  }
}

/** The RPC door for an agent row: profile-routed when the row owns a
 *  connection, else the active gateway. */
const doorFor = row => {
  const route = routeFor(row)
  const request = (method, params = {}) =>
    route && typeof host.requestProfile === 'function'
      ? host.requestProfile(route, method, params)
      : host.request(method, params)
  return { request, route }
}

const rpcErrorText = err => {
  const e = asRecord(err)
  return String(e.message || e.error || err || 'request failed')
}

// ── roster ─────────────────────────────────────────────────────────────────

/** The same roster the Bots pane renders: profiles.list from the active
 *  gateway (rich rows), annotated with host.agents() union rows from other
 *  connections (sparse rows normalized to name = profile || handle). */
async function loadRoster() {
  let rows = []
  try {
    const res = await host.request('profiles.list', {})
    rows = Array.isArray(res?.profiles) ? res.profiles : []
  } catch {
    rows = []
  }

  if (typeof host.agents === 'function') {
    try {
      const union = await host.agents()
      const unionRows = Array.isArray(union?.agents) ? union.agents : []
      const known = new Set(rows.map(row => rowName(row)))
      for (const agent of unionRows) {
        const name = rowName(agent)
        if (name && !known.has(name)) {
          rows.push({ ...agent, name })
          known.add(name)
        }
      }
    } catch {
      /* older build or roster hiccup — profiles.list alone stands */
    }
  }

  return rows
}

// ── reads ──────────────────────────────────────────────────────────────────

/** Current model/provider/reasoning for one session on one agent's door.
 *  `sid` null reads the agent's PROFILE default (what a follow_profile_config
 *  group session rebuilds from — the authoritative value for a member).
 *
 *  Model+provider come from `config.get { key:'provider' }`: the gateway
 *  resolves the profile/session default via _resolve_model() and returns
 *  `{ model, provider, providers }`. `config.get { key:'model' }` has NO
 *  handler and would error, leaving model/provider empty. */
async function readMemberConfig(door, sid) {
  const [modelRes, reasoningRes] = await Promise.allSettled([
    door.request('config.get', sid ? { key: 'provider', session_id: sid } : { key: 'provider' }),
    door.request('config.get', sid ? { key: 'reasoning', session_id: sid } : { key: 'reasoning' })
  ])
  const modelInfo = asRecord(modelRes.status === 'fulfilled' ? modelRes.value : {})
  const reasoningInfo = asRecord(reasoningRes.status === 'fulfilled' ? reasoningRes.value : {})
  return {
    model: String(modelInfo.model || ''),
    provider: String(modelInfo.provider || ''),
    scope: modelInfo.scope === 'session' ? 'session' : 'default',
    reasoning: String(reasoningInfo.value || ''),
    errors: [
      modelRes.status === 'rejected' ? rpcErrorText(modelRes.reason) : null,
      reasoningRes.status === 'rejected' ? rpcErrorText(reasoningRes.reason) : null
    ].filter(Boolean)
  }
}

/** Provider/model catalog for one agent's door — CONFIGURED providers only
 *  (explicit_only), matching the desktop settings model menu. A huge
 *  all-providers catalog makes every picker useless; the user asked for the
 *  providers they've actually set up. */
function fetchModelOptions(door) {
  return door
    .request('model.options', { explicit_only: true })
    .then(res => {
      const providers = (asRecord(res).providers || [])
        .filter(p => p && p.slug)
        .map(p => ({
          slug: String(p.slug),
          name: String(p.name || p.slug),
          models: (p.models || []).map(m => (typeof m === 'string' ? m : String(m.id || m.name || ''))).filter(Boolean)
        }))
      return providers
    })
    .catch(() => [])
}

// ── writes ─────────────────────────────────────────────────────────────────

/**
 * Apply model (+ reasoning) to ONE session, session-scoped — never touches the
 * agent's profile default. Mirrors the app's single-session picker.
 *
 * Resolves { needsConfirm, message, raw } — `raw` is the gateway's model
 * switch response (for the shared confirm flow's confirm_required check);
 * `needsConfirm` is true exactly when the gateway demanded a confirmation the
 * caller has not yet sent.
 */
async function applySessionConfig(door, sid, { model, provider, reasoning }, confirmExpensive = false) {
  let modelResult = null
  if (model && provider) {
    modelResult = asRecord(
      await door.request('config.set', {
        session_id: sid,
        key: 'model',
        value: `${model} --provider ${provider} --session`,
        ...(confirmExpensive ? { confirm_expensive_model: true } : {})
      })
    )
  }
  if (reasoning) {
    await door.request('config.set', { session_id: sid, key: 'reasoning', value: reasoning })
  }
  if (modelResult?.confirm_required && !confirmExpensive) {
    return {
      needsConfirm: true,
      message: String(modelResult.confirm_message || ''),
      raw: modelResult
    }
  }
  return { needsConfirm: false, message: '', raw: modelResult }
}

/** ROOT-CAUSE FIX — durable per-member group write.
 *
 * A group member's model cannot be changed by ``config.set --session`` on the
 * member's plumbing sid. Two independent reasons, both backend-contract, not
 * stale-sid freshness:
 *
 *   1. Group member plumbing sessions are per-turn RUNTIME LEASES, not
 *      residents of the gateway's live ``_sessions`` registry between turns.
 *      ``config.set`` with a non-resident sid falls into the gateway's
 *      non-live else branch (tui_gateway/server.py:14019) and silently no-ops
 *      for the target session.
 *
 *   2. Room plumbing sessions are created with BOTH ``room_plumbing:true`` AND
 *      ``follow_profile_config:true`` (apps/desktop/.../group-turns.ts). The
 *      backend's ``_stored_session_runtime_overrides`` deliberately returns {}
 *      for them (tui_gateway/server.py:5253-5331) — they ALWAYS rebuild from
 *      the member PROFILE's CURRENT config and a session-scoped pin is
 *      DISCARDED on rebuild. So even a resident ``--session`` write would be
 *      thrown away next turn.
 *
 * The durable mechanism is the same one the built-in Bots editor uses to
 * change a bot's model: ``profiles.configure { name, model, provider }``
 * (tui_gateway/methods_profiles.py:749), which writes the member PROFILE's
 * config (``_write_profile_model``) that the follow_profile_config plumbing
 * session then consumes on every rebuild. It needs no live session, so it is
 * immune to residency. It returns the SAME ``confirm_required`` /
 * ``confirm_message`` round-trip shape as ``config.set model`` (and writes
 * nothing until the client resends with ``confirm_expensive_model:true``), so
 * the shared confirm flow is preserved unmodified.
 */
async function applyMemberConfig(door, member, cfg, confirmExpensive = false) {
  const { model, provider, reasoning } = asRecord(cfg)
  const name = memberProfileName(member)
  if (!name || !model || !provider) {
    return { needsConfirm: false, message: '', raw: null }
  }
  const raw = asRecord(
    await door.request('profiles.configure', {
      name,
      model,
      provider,
      ...(confirmExpensive ? { confirm_expensive_model: true } : {})
    })
  )
  if (raw?.confirm_required && !confirmExpensive) {
    return { needsConfirm: true, message: String(raw.confirm_message || ''), raw }
  }
  // Reasoning follows the same profile-default semantics (scope:'global' →
  // agent.reasoning_effort in the profile config), best-effort.
  if (reasoning) {
    try {
      await door.request('config.set', { key: 'reasoning', value: reasoning, scope: 'global' })
    } catch {
      /* reasoning is secondary; a failure must not roll back the model write */
    }
  }
  return { needsConfirm: false, message: '', raw }
}

/** Confirm round-trip for the durable per-member profile write. */
function confirmThenApplyMember(door, member, cfg, pending, onDone) {
  surfaceModelSwitchConfirm({
    confirmLabel: 'Confirm',
    confirmMessage: pending.message,
    failureMessage: 'Model switch failed',
    finish: () => onDone(),
    requestConfirmed: () => applyMemberConfig(door, member, cfg, true).then(r => r.raw || {})
  })
}

// ── rooms (read-only view of Bot Mode's persisted room map) ────────────────

function readRooms() {
  try {
    const raw = window.localStorage.getItem(BOTS_ROOMS_KEY)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

// ── components ─────────────────────────────────────────────────────────────

function Spinner({ label }) {
  return jsx('div', {
    className: 'flex items-center gap-2 py-4 text-xs text-(--ui-text-tertiary)',
    children: [jsx(GlyphSpinner, { className: 'size-3.5 text-(--ui-text-tertiary)', spinner: 'breathe' }), label || 'Loading…']
  })
}

function AvatarDot({ name, color }) {
  const initial = String(name || '?').charAt(0).toUpperCase()
  return jsx('span', {
    className: 'flex size-5 shrink-0 items-center justify-center rounded-full text-[0.6rem] font-bold text-white',
    style: { background: color || '#64748b' },
    children: initial
  })
}

/** Deterministic hue per agent name (no hardcoded palette — theme-safe). */
function hueFor(name) {
  let h = 0
  for (let i = 0; i < name.length; i += 1) {
    h = (h * 31 + name.charCodeAt(i)) % 360
  }
  return `hsl(${h} 55% 45%)`
}

function ReasoningSelect({ value, onChange, disabled }) {
  return jsx(Select, {
    disabled,
    onValueChange: v => onChange(v === NONE ? '' : v),
    value: value || NONE,
    children: [
      jsx(SelectTrigger, {
        className: 'h-7 rounded-md text-xs',
        children: jsx(SelectValue, { placeholder: 'Reasoning' })
      }),
      jsx(SelectContent, {
        children: [
          jsx(SelectItem, { key: NONE, value: NONE, children: 'Reasoning: inherit' }),
          ...REASONING_LEVELS.map(level =>
            jsx(SelectItem, { key: level, value: level, children: `Reasoning: ${REASONING_LABELS[level]}` })
          )
        ]
      })
    ]
  })
}

/** Provider + model controls. Dropdowns when the catalog knows the current
 *  selection; free-text inputs otherwise (same fallback the app uses). */
function ModelControls({ catalog, provider, model, onChange, disabled }) {
  const known = catalog.some(p => p.slug === provider)
  const [freeText, setFreeText] = useState(!known)

  if (!catalog.length || freeText) {
    return jsx('div', {
      className: 'grid grid-cols-2 gap-1.5',
      children: [
        jsx(Input, {
          disabled,
          className: 'h-7 rounded-md text-xs',
          onChange: e => onChange({ provider: e.target.value }),
          placeholder: 'provider',
          value: provider
        }),
        jsx('div', {
          className: 'flex items-center gap-1',
          children: [
            jsx(Input, {
              disabled,
              className: 'h-7 min-w-0 flex-1 rounded-md text-xs',
              onChange: e => onChange({ model: e.target.value }),
              placeholder: 'model',
              value: model
            }),
            catalog.length
              ? jsx(Button, {
                  className: 'h-7 shrink-0 px-1.5 text-xs text-(--ui-text-tertiary)',
                  disabled,
                  onClick: () => setFreeText(false),
                  size: 'sm',
                  variant: 'ghost',
                  children: '▾'
                })
              : null
          ]
        })
      ]
    })
  }

  const active = catalog.find(p => p.slug === provider) || null
  const models = active ? active.models : []
  return jsx('div', {
    className: 'grid grid-cols-2 gap-1.5',
    children: [
      jsx(Select, {
        disabled,
        onValueChange: v => {
          const prov = catalog.find(p => p.slug === v)
          const first = (prov?.models || [])[0] || ''
          onChange({ provider: v, model: models.includes(model) ? model : first })
        },
        value: provider || NONE,
        children: [
          jsx(SelectTrigger, { className: 'h-7 rounded-md text-xs', children: jsx(SelectValue, {}) }),
          jsx(SelectContent, {
            children: [
              jsx(SelectItem, { key: NONE, value: NONE, children: 'Provider: inherit' }),
              ...catalog.map(p => jsx(SelectItem, { key: p.slug, value: p.slug, children: p.name }))
            ]
          })
        ]
      }),
      models.length
        ? jsx(Select, {
            disabled,
            onValueChange: v => onChange({ model: v }),
            value: model || models[0] || NONE,
            children: [
              jsx(SelectTrigger, { className: 'h-7 rounded-md text-xs', children: jsx(SelectValue, {}) }),
              jsx(SelectContent, {
                children: models.map(m => jsx(SelectItem, { key: m, value: m, children: m }))
              })
            ]
          })
        : jsx(Input, {
            disabled,
            className: 'h-7 rounded-md text-xs',
            onChange: e => onChange({ model: e.target.value }),
            placeholder: 'model',
            value: model
          }),
    ]
  })
}

/** Current-state readout — a prominent snapshot of the CURRENT
 *  provider/model/reasoning shown above the edit controls, mirroring the
 *  app's session model selector. The scope badge labels where these values
 *  come from (profile default vs a live session override). */
function CurrentReadout({ provider, model, reasoning, scope }) {
  const isOverride = scope === 'session'
  const label = isOverride ? 'session override' : 'profile default'
  const reason = reasoning ? REASONING_LABELS[reasoning] || reasoning : ''
  const detail = []
  if (model) {
    detail.push(jsx('span', { key: 'p', className: 'text-(--ui-text-secondary)', children: provider || 'default' }))
    detail.push(jsx('span', { key: 'd1', className: 'text-(--ui-text-quaternary)', children: '·' }))
    detail.push(jsx('span', { key: 'm', className: 'font-medium', children: model }))
  } else {
    detail.push(jsx('span', { key: 'm', className: 'font-medium', children: '—' }))
  }
  if (reason) {
    detail.push(jsx('span', { key: 'd2', className: 'text-(--ui-text-quaternary)', children: '·' }))
    detail.push(jsx('span', { key: 'r', children: `Reasoning: ${reason}` }))
  }
  const title = model
    ? `${provider || 'default'} · ${model}${reason ? ` · Reasoning: ${reason}` : ''}`
    : '—'
  return jsx('div', {
    className: 'flex min-w-0 items-center gap-1.5 rounded-md border border-(--ui-stroke-secondary) px-2 py-1',
    children: [
      jsx('span', {
        className: 'shrink-0 text-[0.6rem] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)',
        children: 'Current'
      }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-xs', title, children: detail }),
      jsx(Badge, {
        className: 'shrink-0 text-[0.6rem]',
        variant: isOverride ? 'default' : 'muted',
        children: label
      })
    ]
  })
}

// ── Groups tab ─────────────────────────────────────────────────────────────

function GroupsTab({ rooms, rosterByName, refreshEpoch }) {
  const groupNames = useMemo(() => Object.keys(rooms || {}).filter(n => !rooms[n]?.tombstone).sort(), [rooms])
  const [group, setGroup] = useState('')
  useEffect(() => {
    if (!group && groupNames.length) {
      setGroup(groupNames[0])
    }
  }, [group, groupNames])
  const room = group ? rooms?.[group] : null
  const members = (room?.members || []).slice(0, MAX_GROUP_MEMBERS)
  const memberRows = members.map(member => {
    const row = rosterByName[memberKey(member)] || rosterByName[member.name || member.profile] || member
    return { member, row }
  })
  const truncated = (room?.members || []).length > MAX_GROUP_MEMBERS

  if (!groupNames.length) {
    return jsx(EmptyState, {
      title: 'No group chats',
      description: 'Create a Bot Mode group chat first — member models show up here.',
      icon: jsx(Codicon, { name: 'organization' })
    })
  }

  return jsx('div', {
    className: 'grid gap-2 p-2',
    children: [
      jsx(Select, {
        onValueChange: setGroup,
        value: group,
        children: [
          jsx(SelectTrigger, { className: 'h-8 rounded-md text-xs', children: jsx(SelectValue, {}) }),
          jsx(SelectContent, {
            children: groupNames.map(name => jsx(SelectItem, { key: name, value: name, children: name }))
          })
        ]
      }),
      truncated
        ? jsx('div', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            children: `Showing ${MAX_GROUP_MEMBERS} of ${room.members.length} members`
          })
        : null,
      ...memberRows.map(({ member, row }) =>
        jsx(MemberRow, { key: memberKey(member), member, row, room, group, refreshEpoch })
      ),
      memberRows.length > 1
        ? jsx(AllMembersRow, { key: 'all', memberRows, room, group, refreshEpoch })
        : null
    ]
  })
}

/** One set of controls below the member rows: set provider/model/reasoning
 *  once and Apply pushes it to EVERY member with a live group session. */
function AllMembersRow({ memberRows, room, group, refreshEpoch }) {
  const [draft, setDraft] = useState(null)
  const [applying, setApplying] = useState(false)
  const [msg, setMsg] = useState('')
  const [catalog, setCatalog] = useState([])

  // Seed from the FIRST member's door (the catalog is shared across the
  // active gateway in practice); each apply routes per-member anyway.
  useEffect(() => {
    let alive = true
    const first = memberRows.find(entry => typeof room?.sessions?.[memberKey(entry.member)] === 'string')
    if (!first) {
      return undefined
    }
    const door = doorFor(first.row)
    Promise.all([readMemberConfig(door, null), fetchModelOptions(door)])
      .then(([cfg, cats]) => {
        if (!alive) {
          return
        }
        setCatalog(cats)
        setDraft(d => d || { model: cfg.model, provider: cfg.provider, reasoning: cfg.reasoning || 'medium' })
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [memberRows, room, refreshEpoch])

  const applyAll = async () => {
    if (!draft?.model || !draft?.provider) {
      return
    }
    // Group member plumbing sessions are follow_profile_config: they rebuild
    // from each member PROFILE's current config on every resume, and a
    // session-scoped config.set --session pin is discarded. The durable write
    // targets each member PROFILE default via profiles.configure — no plumbing
    // sid, no residency requirement. Every member with a profile name is
    // targeted.
    const targets = memberRows
      .map(entry => ({ door: doorFor(entry.row), member: entry.member }))
      .filter(t => memberProfileName(t.member))
    if (!targets.length) {
      setMsg('No member profiles found')
      return
    }
    setApplying(true)
    setMsg('')
    try {
      const results = await Promise.all(
        targets.map(async t => {
          try {
            const pending = await applyMemberConfig(t.door, t.member, draft)
            return pending.needsConfirm ? { needsConfirm: true, message: pending.message } : { ok: true }
          } catch (err) {
            return { error: rpcErrorText(err) }
          }
        })
      )
      const confirmPending = results.find(r => r.needsConfirm)
      if (confirmPending) {
        surfaceModelSwitchConfirm({
          confirmLabel: 'Confirm',
          confirmMessage: confirmPending.message,
          failureMessage: 'Model switch failed',
          finish: () => setMsg('Applied to all members'),
          requestConfirmed: () =>
            Promise.all(
              targets.map(async t => {
                try {
                  const pending = await applyMemberConfig(t.door, t.member, draft, true)
                  return pending.needsConfirm ? { needsConfirm: true, message: pending.message } : { ok: true }
                } catch (err) {
                  return { error: rpcErrorText(err) }
                }
              })
            ).then(redos => {
              const still = redos.find(r => r.needsConfirm)
              if (still) {
                return { confirm_required: true, confirm_message: still.message }
              }
              const failed = redos.filter(r => r.error).length
              setMsg(`Applied to ${redos.length - failed}/${redos.length} members`)
              return { ok: true }
            })
        })
        setApplying(false)
        return
      }
      const failed = results.filter(r => r.error)
      setMsg(
        failed.length
          ? `Applied to ${results.length - failed.length}/${results.length} members`
          : `Applied to ${results.length} members`
      )
    } catch (err) {
      setMsg(`Failed: ${rpcErrorText(err)}`)
    }
    setApplying(false)
  }

  return jsx('div', {
    className: 'grid gap-1.5 rounded-md border border-dashed border-(--ui-accent) p-2',
    children: [
      jsx('div', {
        className: 'text-xs font-semibold text-(--ui-accent)',
        children: `Apply to all ${memberRows.length} members`
      }),
      jsx(ModelControls, {
        catalog,
        disabled: applying,
        model: draft?.model ?? '',
        onChange: patch => setDraft(d => ({ ...(d || {}), ...patch })),
        provider: draft?.provider ?? ''
      }),
      jsx(ReasoningSelect, {
        disabled: applying,
        onChange: v => setDraft(d => ({ ...(d || {}), reasoning: v })),
        value: draft?.reasoning ?? ''
      }),
      jsx(Button, {
        className: 'h-7 text-xs',
        disabled: applying || !draft?.model || !draft?.provider,
        onClick: () => void applyAll(),
        size: 'sm',
        children: applying ? 'Applying…' : 'Apply to all members'
      }),
      msg ? jsx('div', { className: 'text-[0.65rem] text-(--ui-text-secondary)', children: msg }) : null
    ]
  })
}

function MemberRow({ member, row, room, group, refreshEpoch }) {
  const sid = typeof room?.sessions?.[memberKey(member)] === 'string' ? room.sessions[memberKey(member)] : null
  const door = useMemo(() => doorFor(row), [row])
  const [state, setState] = useState({
    loading: true,
    error: '',
    model: '',
    provider: '',
    reasoning: '',
    scope: 'default',
    catalog: [],
    applying: false,
    liveSid: null
  })
  const [draft, setDraft] = useState(null)
  const [applyMsg, setApplyMsg] = useState('')

  useEffect(() => {
    let alive = true
    setState(s => ({ ...s, loading: true, error: '' }))
    // The AUTHORITATIVE current config for a group member IS its PROFILE
    // default (readMemberConfig with sid null): member plumbing sessions are
    // follow_profile_config and rebuild from that default on every resume, so
    // a session-scoped read would be stale/non-authoritative. resolveLiveSessionId
    // is kept ONLY to learn the live sid for the session.info overlay below —
    // never as the handle to read config through.
    Promise.all([
      resolveLiveSessionId(door, member, group, room),
      readMemberConfig(door, null),
      fetchModelOptions(door)
    ]).then(([live, cfg, catalog]) => {
      if (!alive) {
        return
      }
      setState(s => ({ ...s, loading: false, ...cfg, catalog, liveSid: live.sid }))
      setDraft({ model: cfg.model, provider: cfg.provider, reasoning: cfg.reasoning || 'medium' })
    })
      .catch(err => {
        if (alive) {
          setState(s => ({ ...s, loading: false, error: rpcErrorText(err) }))
        }
      })
    return () => {
      alive = false
    }
  }, [door, sid, group, refreshEpoch])

  // Live truth: session.info events for this member's session repaint the row.
  // Filter on the RESOLVED live sid (state.liveSid), not the possibly-stale
  // persisted sid — a reaped session never emits, and the live one would miss.
  useEffect(() => {
    if (!state.liveSid) {
      return undefined
    }
    return host.onEvent('session.info', event => {
      if (event?.session_id !== state.liveSid) {
        return
      }
      const payload = asRecord(event.payload)
      const patch = {}
      if (typeof payload.model === 'string') {
        patch.model = payload.model
        patch.scope = 'session'
      }
      if (typeof payload.provider === 'string') {
        patch.provider = payload.provider
      }
      if (typeof payload.reasoning_effort === 'string') {
        patch.reasoning = payload.reasoning_effort
      }
      if (Object.keys(patch).length) {
        setState(s => ({ ...s, ...patch }))
        setDraft(d => (d ? { ...d, ...patch } : d))
      }
    })
  }, [state.liveSid])

  const apply = async () => {
    if (!draft?.model || !draft?.provider) {
      return
    }
    // Group members run follow_profile_config plumbing sessions: they rebuild
    // from the member PROFILE's CURRENT config on every resume, and a
    // session-scoped config.set --session pin on the plumbing sid is discarded
    // (and the sid is not even resident in the gateway between turns, so the
    // write silently no-ops). The durable per-member write therefore targets
    // the member PROFILE default via profiles.configure — never a plumbing sid.
    const name = memberProfileName(member)
    if (!name) {
      setApplyMsg('No profile for this member')
      return
    }
    setState(s => ({ ...s, applying: true }))
    setApplyMsg('')
    try {
      const pending = await applyMemberConfig(door, member, draft)
      if (pending.needsConfirm) {
        confirmThenApplyMember(door, member, draft, pending, () => {
          setApplyMsg('Applied — takes effect on the member\'s next turn')
          setState(s => ({ ...s, applying: false }))
        })
        return
      }
      // Read back the PROFILE default (sid null) so the row shows exactly what
      // the member's next group turn actually runs.
      const readback = await readMemberConfig(door, null)
      setState(s => ({
        ...s,
        applying: false,
        model: readback.model || draft.model,
        provider: readback.provider || draft.provider,
        reasoning: readback.reasoning || draft.reasoning,
        scope: 'default'
      }))
      setApplyMsg('Applied — takes effect on the member\'s next turn')
    } catch (err) {
      setApplyMsg(`Failed: ${rpcErrorText(err)}`)
      setState(s => ({ ...s, applying: false }))
    }
  }
  const name = displayNameOf(row)
  const color = hueFor(String(member.name || rowName(row) || name))

  if (state.loading) {
    return jsx('div', {
      className: 'rounded-md border border-(--ui-stroke-secondary) p-2',
      children: jsx(Spinner, { label: `${name}…` })
    })
  }

  return jsx('div', {
    className: 'grid gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(AvatarDot, { color, name }),
          jsx('span', { className: 'min-w-0 flex-1 truncate text-xs font-semibold', children: name })
        ]
      }),
      state.error
        ? jsx('div', { className: 'text-[0.65rem] text-red-500', children: state.error })
        : null,
      jsx(CurrentReadout, {
        provider: state.provider,
        model: state.model,
        reasoning: state.reasoning,
        scope: state.scope
      }),
      !sid
        ? jsx('div', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            children: 'No session yet — send the first message, then this member can be configured.'
          })
        : jsx('div', {
            className: 'grid gap-1.5',
            children: [
              jsx('div', {
                className: 'text-[0.6rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                children: 'Change'
              }),
              jsx(ModelControls, {
                catalog: state.catalog,
                disabled: state.applying,
                model: draft?.model ?? state.model,
                onChange: patch => setDraft(d => ({ ...(d || {}), ...patch })),
                provider: draft?.provider ?? state.provider
              }),
              jsx('div', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx(ReasoningSelect, {
                    disabled: state.applying,
                    onChange: v => setDraft(d => ({ ...(d || {}), reasoning: v })),
                    value: draft?.reasoning ?? state.reasoning
                  }),
                  jsx(Button, {
                    className: 'h-7 flex-1 text-xs',
                    disabled: state.applying || !draft?.model || !draft?.provider,
                    onClick: () => void apply(),
                    size: 'sm',
                    children: state.applying ? 'Applying…' : 'Apply'
                  })
                ]
              }),
              applyMsg ? jsx('div', { className: 'text-[0.65rem] text-(--ui-text-secondary)', children: applyMsg }) : null
            ]
          })
    ]
  })
}

// ── Agents tab ─────────────────────────────────────────────────────────────

function AgentsTab({ agents, refreshEpoch }) {
  const list = (agents || [])
    .filter(a => rowName(a) && !a?.ghost)
    .sort((a, b) => rowName(a).localeCompare(rowName(b)))
  const [agent, setAgent] = useState('')
  useEffect(() => {
    if (!agent && list.length) {
      setAgent(rowName(list[0]))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, list.length])
  const row = list.find(a => rowName(a) === agent) || null

  if (!list.length) {
    return jsx(EmptyState, {
      title: 'No agents',
      description: 'Create an agent profile first — then sync its model everywhere.',
      icon: jsx(Codicon, { name: 'bot' })
    })
  }

  return jsx('div', {
    className: 'grid gap-2 p-2',
    children: [
      jsx(Select, {
        onValueChange: setAgent,
        value: agent,
        children: [
          jsx(SelectTrigger, { className: 'h-8 rounded-md text-xs', children: jsx(SelectValue, {}) }),
          jsx(SelectContent, {
            children: list.map(a => jsx(SelectItem, { key: rowName(a), value: rowName(a), children: displayNameOf(a) }))
          })
        ]
      }),
      row ? jsx(AgentRow, { key: rowName(row), row, refreshEpoch }) : null
    ]
  })
}

function AgentRow({ row, refreshEpoch }) {
  const door = useMemo(() => doorFor(row), [row])
  const [state, setState] = useState({
    loading: true,
    error: '',
    model: '',
    provider: '',
    reasoning: '',
    catalog: [],
    sessionCount: null
  })
  const [draft, setDraft] = useState(null)
  const [syncing, setSyncing] = useState(false)
  const [syncMsg, setSyncMsg] = useState('')

  useEffect(() => {
    let alive = true
    setState(s => ({ ...s, loading: true, error: '' }))
    Promise.all([readMemberConfig(door, null), fetchModelOptions(door)])
      .then(([cfg, catalog]) => {
        if (!alive) {
          return
        }
        setState(s => ({ ...s, loading: false, ...cfg, catalog }))
        setDraft({ model: cfg.model, provider: cfg.provider, reasoning: cfg.reasoning || 'medium' })
      })
      .catch(err => {
        if (alive) {
          setState(s => ({ ...s, loading: false, error: rpcErrorText(err) }))
        }
      })
    return () => {
      alive = false
    }
  }, [door, refreshEpoch])

  const countSessions = async () => {
    try {
      const res = await door.request('session.list', { include_hidden: true, limit: 500 })
      const sessions = (asRecord(res).sessions || []).filter(s => typeof s?.id === 'string' && s.id)
      setState(s => ({ ...s, sessionCount: sessions.length }))
      return sessions
    } catch {
      return null
    }
  }

  const sync = async () => {
    if (!draft?.model || !draft?.provider) {
      return
    }
    setSyncing(true)
    setSyncMsg('')
    try {
      const sessions = await countSessions()
      if (!sessions) {
        setSyncMsg('Could not list sessions')
        setSyncing(false)
        return
      }
      if (!sessions.length) {
        setSyncMsg('No sessions to sync')
        setSyncing(false)
        return
      }
      const results = await Promise.all(
        sessions.map(async s => {
          try {
            const pending = await applySessionConfig(door, s.id, draft)
            return pending.needsConfirm
              ? { id: s.id, title: s.title, needsConfirm: true, message: pending.message }
              : { id: s.id, title: s.title, ok: true }
          } catch (err) {
            return { id: s.id, title: s.title, error: rpcErrorText(err) }
          }
        })
      )
      const confirmPending = results.find(r => r.needsConfirm)
      if (confirmPending) {
        // One shared confirm for the batch; on confirm, re-run the whole sync
        // with the flag so the expensive-model guard is satisfied everywhere.
        surfaceModelSwitchConfirm({
          confirmLabel: 'Confirm',
          confirmMessage: confirmPending.message,
          failureMessage: 'Model switch failed',
          finish: () => setSyncMsg('Sync applied'),
          requestConfirmed: () =>
            Promise.all(
              sessions.map(async s => {
                try {
                  const pending = await applySessionConfig(door, s.id, draft, true)
                  return pending.needsConfirm ? { needsConfirm: true, message: pending.message } : { ok: true }
                } catch (err) {
                  return { error: rpcErrorText(err) }
                }
              })
            ).then(redos => {
              const still = redos.find(r => r.needsConfirm)
              if (still) {
                // The wrapper treats a still-confirm_required resend as failure.
                return { confirm_required: true, confirm_message: still.message }
              }
              const failed = redos.filter(r => r.error).length
              setSyncMsg(`Synced ${redos.length - failed}/${redos.length} sessions`)
              return { ok: true }
            })
        })
        setSyncing(false)
        return
      }
      const failed = results.filter(r => r.error)
      setSyncMsg(
        failed.length
          ? `Synced ${results.length - failed.length}/${results.length} sessions`
          : `Synced ${results.length} sessions`
      )
    } catch (err) {
      setSyncMsg(`Sync failed: ${rpcErrorText(err)}`)
    }
    setSyncing(false)
  }

  const name = displayNameOf(row)
  const color = hueFor(String(rowName(row) || name))

  if (state.loading) {
    return jsx(Spinner, { label: `${name}…` })
  }

  return jsx('div', {
    className: 'grid gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(AvatarDot, { color, name }),
          jsx('span', { className: 'min-w-0 flex-1 truncate text-xs font-semibold', children: name })
        ]
      }),
      state.error
        ? jsx('div', { className: 'text-[0.65rem] text-red-500', children: state.error })
        : null,
      jsx(CurrentReadout, {
        provider: state.provider,
        model: state.model,
        reasoning: state.reasoning,
        scope: 'default'
      }),
      jsx('div', {
        className: 'text-[0.6rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
        children: 'Change'
      }),
      jsx(ModelControls, {
        catalog: state.catalog,
        disabled: syncing,
        model: draft?.model ?? state.model,
        onChange: patch => setDraft(d => ({ ...(d || {}), ...patch })),
        provider: draft?.provider ?? state.provider
      }),
      jsx(ReasoningSelect, {
        disabled: syncing,
        onChange: v => setDraft(d => ({ ...(d || {}), reasoning: v })),
        value: draft?.reasoning ?? state.reasoning
      }),
      jsx(Button, {
        className: 'h-7 text-xs',
        disabled: syncing || !draft?.model || !draft?.provider,
        onClick: () => void sync(),
        size: 'sm',
        children: syncing ? 'Syncing…' : 'Sync model + reasoning to all sessions'
      }),
      syncMsg ? jsx('div', { className: 'text-[0.65rem] text-(--ui-text-secondary)', children: syncMsg }) : null,
      state.sessionCount !== null && !syncMsg
        ? jsx('div', {
            className: 'text-[0.65rem] text-(--ui-text-quaternary)',
            children: `${state.sessionCount} existing session${state.sessionCount === 1 ? '' : 's'}`
          })
        : null
    ]
  })
}

// ── pane root ──────────────────────────────────────────────────────────────

export default {
  id: ID,
  name: 'Model Sync',
  version: '1.0.2',
  description: 'Per-member model selector for group chats + push an agent profile model to all of its sessions.',

  register(ctx) {
    ctx.register({
      id: 'model-sync-pane',
      area: 'panes',
      title: 'Model Sync',
      data: {
        placement: 'right',
        width: 360
      },
      render: () => jsx(PaneRoot, {})
    })
  }
}

function PaneRoot() {
  const [tab, setTab] = useState('groups')
  const [refreshEpoch, setRefreshEpoch] = useState(0)
  const [roster, setRoster] = useState(null)
  const [rosterError, setRosterError] = useState('')
  const rooms = useMemo(() => readRooms(), [refreshEpoch])
  const rosterByName = useMemo(() => {
    const map = {}
    for (const row of roster || []) {
      const name = rowName(row)
      if (name) {
        map[name] = row
      }
      const key = memberKey(row)
      if (key) {
        map[key] = row
      }
      if (row?.connectionId && name) {
        map[`${row.connectionId}::${name}`] = row
      }
    }
    return map
  }, [roster])

  useEffect(() => {
    let alive = true
    setRosterError('')
    loadRoster()
      .then(rows => {
        if (alive) {
          setRoster(rows)
        }
      })
      .catch(err => {
        if (alive) {
          setRosterError(rpcErrorText(err))
        }
      })
    return () => {
      alive = false
    }
  }, [refreshEpoch])

  const refresh = () => setRefreshEpoch(e => e + 1)

  return jsx('div', {
    className: 'flex h-full flex-col',
    children: [
      jsx('div', {
        className: 'flex items-center justify-between gap-2 border-b border-(--ui-stroke-secondary) px-2 py-1.5',
        children: [
          jsx('div', {
            className: 'flex items-center gap-1',
            children: [
              jsx(Button, {
                className: 'h-6 px-2 text-xs',
                onClick: () => setTab('groups'),
                size: 'sm',
                variant: tab === 'groups' ? 'secondary' : 'ghost',
                children: 'Groups'
              }),
              jsx(Button, {
                className: 'h-6 px-2 text-xs',
                onClick: () => setTab('agents'),
                size: 'sm',
                variant: tab === 'agents' ? 'secondary' : 'ghost',
                children: 'Agents'
              })
            ]
          }),
          jsx(Tip, {
            label: 'Refresh (re-read rooms, roster, and current models)',
            children: jsx(Button, {
              'aria-label': 'Refresh',
              className: 'shrink-0 text-(--ui-text-tertiary) hover:text-foreground',
              onClick: refresh,
              size: 'sm',
              variant: 'ghost',
              children: jsx(Codicon, { name: 'refresh' })
            })
          })
        ]
      }),
      rosterError
        ? jsx('div', { className: 'px-2 py-3 text-xs text-red-500', children: rosterError })
        : roster === null
          ? jsx(Spinner, { label: 'Loading agents…' })
          : tab === 'groups'
            ? jsx(GroupsTab, { rooms, rosterByName, refreshEpoch })
            : jsx(AgentsTab, { agents: roster, refreshEpoch })
    ]
  })
}
