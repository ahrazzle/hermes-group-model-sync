# group-model-sync

Per-member model & reasoning selector for Bot Mode group chats in **Hermes Desktop**, plus agent-wide model sync across every session of a profile.

Two features, one right-side pane:

- **Groups tab** — pick a group chat and see every member agent's current model + reasoning level, then change either per member (up to 6 members). Same provider/model dropdowns the app's single-session picker uses, duplicated per member, backed by the same gateway RPCs.
- **Agents tab** — pick an agent profile, set model + reasoning, and press **Sync** to push that configuration to every existing session of that agent (canonical Bot Chat, group plumbing sessions, cron sessions) — exactly as if you had opened each session and picked the model manually.

## Why this exists

Hermes Desktop lets you switch a model per session, but there is no surface to:

1. See — at a glance — what model/reasoning each agent in a Bot Mode group chat is currently running on, and change them independently.
2. Push a profile's model configuration to all of that profile's existing sessions at once.

This plugin fills both gaps with the app's own RPC contract (`config.set --session`, `config.get`, `model.options`) — no forked build, no patched bundle, no telemetry.

## Requirements

- Hermes Desktop with Bot Mode group chats (bundled `hermes-bots` plugin).
- Backend support for session-scoped model/reasoning override persistence across idle reaping (the `session_override` marker in `tui_gateway`) — shipped via [PR #98901](https://github.com/NousResearch/hermes-agent/pull/98901). Without that PR, applies still work on live sessions but do not survive an idle reaper; reads fall back to the profile default for reaped sessions.

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

Session-scoped applies pin that session only — the profile default is never touched. The pane labels pinned sessions with a **session override** badge.

## Package layout

```
hermes-group-model-sync/
├── plugin.yaml          # Agent-plugin manifest (this repo is a unified package)
├── desktop/
│   └── plugin.js        # Desktop plugin (ESM, loaded by Hermes Desktop)
└── README.md
```

## How it works

- **Roster**: `profiles.list` on the active gateway (the same rich rows the Bots pane renders), plus `host.agents()` union rows for other connections.
- **Room map**: reads Bot Mode's persisted room records (localStorage key `hermes.plugin.hermes-bots.group-chats`) read-only — degrades gracefully if the format changes.
- **RPCs**: routed to each agent's own source via `host.requestProfile`, or the active gateway via `host.request` when a row has no route.
- **Write**: `config.set {session_id, key:'model', value:'<model> --provider <provider> --session'}` and `config.set {session_id, key:'reasoning', value:'<level>'}` — session-scoped, never global.
- **Read**: `config.get {key:'model', session_id}` (session-aware) and `config.get {key:'reasoning', session_id}`.

All data is local. No telemetry, no network beyond the app's own gateway.

## License

MIT