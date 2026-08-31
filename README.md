# group-model-sync

Per-member model & reasoning selector for Bot Mode group chats in **Hermes Desktop**, plus agent-wide model sync across every session of a profile.

Two features, one right-side pane:

- **Groups tab** — pick a group chat and see every member agent's current model + reasoning level, then change either per member (up to 6 members). Same provider/model dropdowns the app's single-session picker uses, duplicated per member, backed by the same gateway RPCs.
- **Agents tab** — pick an agent profile, set model + reasoning, and press **Sync** to push that configuration to every existing session of that agent (canonical Bot Chat, group plumbing sessions, cron sessions) — exactly as if you had opened each session and picked the model manually.

## Why this exists

Hermes Desktop lets you switch a model per session, but there is no surface to:

1. See — at a glance — what model/reasoning each agent in a Bot Mode group chat is currently running on, and change them independently.
2. Push a profile's model configuration to all of that profile's existing sessions at once.

This plugin fills both gaps with the app's own RPC contract (`profiles.configure`, `config.get`, `model.options`) — no forked build, no patched bundle, no telemetry.

## Requirements

- Hermes Desktop with Bot Mode group chats (bundled `hermes-bots` plugin).
- Backend support for `profiles.configure` (writes a profile's model default) — standard in current Hermes builds.

## Install

**Unified package (recommended):**
```
hermes plugins install ahrazzle/hermes-group-model-sync
```
The plugin inventories in **Settings → Plugins** and is disabled by default. Enable it to activate the desktop pane.

**Disk install (manual):**
```
cp -r desktop ~/.hermes/desktop-plugins/group-model-sync
```
The app hot-reloads standalone plugins — save the file and the Model Sync pane appears within seconds. Verify via ⌘K palette: type "Model Sync" and it shows under the PLUGINS header.

## Usage

1. Open the **Model Sync** pane (right sidebar).
2. **Groups tab**: pick a group from the dropdown. Each member row shows its current provider/model/reasoning. Change any control and hit **Apply**. An "Apply to all N members" row below sets one provider/model/reasoning for the whole room.
3. **Agents tab**: pick a profile, set model + reasoning, hit **Sync** to push to every session of that agent.

## Known limitations

- **A per-group model change applies to the member in every group.** The Groups-tab Apply writes the member's *profile* default (`profiles.configure {name, model, provider}`), because a group member's plumbing sessions are `follow_profile_config` — they rebuild from that profile default on every resume, and a session-scoped `config.set --session` pin is discarded. A side effect is that the change is not scoped to the one room you edited it in: it changes the member's model in all of that agent's group chats (and its default). This is a known tradeoff of the current implementation. A fully room-scoped per-member pin would require a backend change to persist a durable per-room override for `follow_profile_config` sessions; that is on the roadmap for official integration into Hermes. The Agents-tab Sync is intentionally global (it pushes to every session of the profile by design).

## Package layout

```
hermes-group-model-sync/
├── plugin.yaml          # Agent-plugin manifest (this repo is a unified package)
├── desktop/
│   └── plugin.js        # Desktop plugin (ESM, loaded by Hermes Desktop)
├── test/
│   ├── resolver.test.js    # Dependency-free node test for the live-session resolver
│   ├── apply-member.test.js# Dependency-free node test for the durable per-member write
│   └── read-default.test.js# Dependency-free node test for the authoritative profile-default read
└── README.md
```

Run the tests with `node test/resolver.test.js`, `node test/apply-member.test.js`, and `node test/read-default.test.js` (no npm dependencies).

## How it works

- **Roster**: `profiles.list` on the active gateway (the same rich rows the Bots pane renders), plus `host.agents()` union rows for other connections.
- **Room map**: reads Bot Mode's persisted room records (localStorage key `hermes.plugin.hermes-bots.group-chats`) read-only — degrades gracefully if the format changes.
- **Group-member write (durable)**: a group member's model CANNOT be changed by `config.set --session` on the member's plumbing session. Two backend contracts make that a guaranteed no-op:
  1. Bot-Mode room plumbing sessions are per-turn runtime leases, not residents of the gateway's live session registry between turns, so `config.set` against that sid falls into the non-live branch and silently no-ops.
  2. They are created with `room_plumbing` + `follow_profile_config`, so the backend always rebuilds them from the member PROFILE's current config and discards any session-scoped pin on resume.
  The durable per-member write therefore goes through `profiles.configure { name, model, provider }` on the member's door — the same RPC the built-in Bots editor uses — which writes the member profile's model default that the plumbing session follows on every rebuild. It preserves the same `confirm_required`/`confirm_message` round-trip as `config.set model` (resend with `confirm_expensive_model: true` to confirm a guarded pick).
- **Agents-tab sync**: enumerates the profile's sessions and pins each via the app's session-scoped path, using live-session resolution (`session.list`, matching the room's `Group: <roomId|name>` title) instead of trusting the persisted room-map sid (which can point at a ws-orphan-reaped session).
- **RPCs**: routed to each agent's own source via `host.requestProfile`, or the active gateway via `host.request` when a row has no route.
- **Read**: `config.get {key:'provider'}` (returns the resolved `{model, provider}` of the profile default) and `config.get {key:'reasoning'}` against the profile default, plus live `session.info` events for the member's resolved live session. (`config.get {key:'model'}` has no gateway handler — the old read path was a silent no-op.)

All data is local. No telemetry, no network beyond the app's own gateway.

## License

MIT