# group-model-sync

Model control for a Hermes fleet: per-member model & reasoning for Bot Mode group chats, agent-wide session sync, and fleet-wide provider/model **presets** applied to named profiles — from the CLI or from one right-side desktop pane.

Three surfaces, one package:

| Surface | What it is | Writes |
|---|---|---|
| `hermes group-model-sync …` | CLI verbs (`preset`, `plan`, `apply`, `verify`, `rollback`, `doctor`) | a named profile's `config.yaml`, with a backup and a read-back |
| **Model Sync pane** (right sidebar) | desktop pane, three tabs: Groups, Agents, Presets | Groups/Agents via the app's own gateway RPCs; Presets via this plugin's own backend route |
| `/gms status` | in-session command (CLI and gateway) | nothing — read-only by design |

There is deliberately **no agent tool**: a model-facing tool that can rewrite `config.yaml` is a mis-invocation risk with no upside. Presets are applied by a human act — the CLI, or the pane.

## Why this exists

Hermes Desktop switches a model per session, and the Dashboard switches one profile's default. Nothing shows what each agent in a group chat is running, nothing pushes one configuration to every session of an agent, and nothing applies a named provider/model preset across profiles. This plugin fills those three gaps:

1. **Groups tab** — every member agent's current model + reasoning in a Bot Mode room, changeable per member (up to 6 members per room).
2. **Agents tab** — push one profile's model + reasoning to every existing session of that agent (canonical Bot Chat, group plumbing sessions, cron sessions), exactly as if each session had been switched by hand.
3. **Presets tab** — apply a named preset (provider, model, base_url, api_mode, reasoning effort, fallback chain) to explicitly chosen profiles, with a diff preview before anything is written.

## Requirements

- Hermes with plugin support (agent half: CLI + dashboard routes; desktop half: Hermes Desktop).
- For the Groups and Agents tabs: Hermes Desktop with Bot Mode group chats (bundled `hermes-bots` plugin) and backend support for `profiles.configure`.
- Nothing else: the agent half runs on the stock Hermes Python (stdlib + the PyYAML Hermes already ships). No pip dependency, no lazy install, no network call of its own.

## Install

Install **per profile** — writes are profile-native, so the habit is one install per profile:

```bash
# replace <profile> with each profile you want this in
hermes -p <profile> plugins install ahrazzle/hermes-group-model-sync --ref <40-char-commit-sha>
hermes -p <profile> plugins enable hermes-group-model-sync
```

Then flip the **desktop half** on in **Settings → Plugins**. `install` ≠ active: both halves are opt-in and off by default (the Python half via `plugins.enabled`, the desktop half via the Settings toggle), and the desktop half hot-reloads on save once enabled.

Pinning `--ref <sha>` is deliberate: an upgrade becomes an explicit choice of a new commit.

- **Upgrade:** `hermes -p <profile> plugins update hermes-group-model-sync` (refused while pinned — intended). The desktop half needs no second command: the runtime loader watches its folder and reloads it in place.
- **Uninstall:** `hermes -p <profile> plugins remove hermes-group-model-sync` (and turn the desktop half off in Settings → Plugins). Removing the install folder does **not** touch any profile's `config.yaml` or any backup.
- **Distributed copies:** `presets/presets.json` lives inside the install folder, so an upgrade replaces it. Keep your own preset values in your own copy of this repository.

## Presets

A preset is a named group of settings held in one file that both halves read (`presets/presets.json`); the format and the rules are documented in `presets/README.md`. That installed file is the **only** catalog v1 reads — `plugin.yaml` declares no alternative-catalog key.

```bash
hermes group-model-sync preset list                           # ids + one-line descriptions
hermes group-model-sync preset show fleet-default             # every key that preset names
hermes group-model-sync plan  fleet-default --profiles a,b     # read-only diff; writes nothing
hermes group-model-sync apply fleet-default --profiles a,b --yes --verify
hermes group-model-sync verify --profiles a,b                  # read-back only (config + state.db)
hermes group-model-sync rollback --profile a                   # restore the newest backup
hermes group-model-sync doctor                                 # env, profiles, schema, version drift
```

Add `--json` to any verb for a machine-readable receipt.

The rules that make this safe:

- **A preset only touches six keys:** `model.provider`, `model.default`, `model.base_url`, `model.api_mode`, `agent.reasoning_effort`, `fallback_providers`. Nothing else is ever in scope.
- **Absent ≠ null ≠ "clear it".** A key the preset does not name is never written. There is no way for a preset to blank a key.
- **Only differing keys are written**, and a second identical apply writes nothing and says so.
- **`apply` needs an explicit target and `--yes`.** Without `--yes` it prints the plan and exits non-zero; `--dry-run` prints the plan and exits 0. It refuses an unknown preset, a profile that does not exist, a profile with no `config.yaml`, and an unparseable `config.yaml`.
- **Every write is backed up and read back.** The current file is copied to `config.yaml.bak-<UTC>-<preset id>` first; afterwards every changed key is read back from the file. A write that does not read back as intended is a failure, not a warning. `rollback` restores a backup (taking its own safety copy first) and re-verifies.
- **`--verify` reads the target's `state.db`** for the declared model's usage rows. "No usage rows yet" is reported as UNVERIFIED, never as success; add `--require-verified` to make that fatal.
- **Future sessions only.** An apply changes `config.yaml`; a session already running keeps the model it started with. Pinning an existing session is a separate, explicit desktop action (**Model Sync → Agents**) — the CLI half deliberately has no verb for it, and it is never a side effect of a preset.
- **Guarded presets** (`"guarded": true`) need `--confirm-expensive` (CLI) or the confirm dialog (pane) before they will write.
- **A preset with per-profile `assignments`** (a named fleet array: one route per profile) applies only to the profiles it names. A target it does not name is refused with the assigned names listed — presets never guess a route. See `presets/README.md`.

### The shipped presets

`fleet-default` and `minimal` are labelled **EXAMPLE ONLY** in their name and description. They carry illustrative machine-specific values (an opencode-go route and a direct-deepseek route) so that `plan` has something real to diff against on a first run. They are not fleet defaults and not a recommendation, and their provider/model/url/fallback values may be stale or invalid on another install. Replace every value with your own before applying.

`config-3` is a **real per-profile array** — seven fleet profiles with three distinct routes, read 2026-09-17, expressed with `assignments` — so you can see the fleet-array shape working data takes. It is guarded (one route names a model the gateway may treat as expensive), it names no reasoning effort and no fallback chain, and it refuses every profile outside its seven. Replace it with your own array before relying on it.

## Usage

1. Open the **Model Sync** pane (right sidebar).
2. **Groups tab**: pick a group from the dropdown. Each member row shows its current provider/model/reasoning. Change any control and hit **Apply**. An "Apply to all N members" row below sets one provider/model/reasoning for the whole room.
3. **Agents tab**: pick a profile, set model + reasoning, hit **Sync** to push to every session of that agent.
4. **Presets tab**: pick a preset, tick the target profiles, **Preview diff**, then **Apply** and confirm. The receipt shows the changed keys, the read-back rows, the backup path and the `state.db` verdict.

## Package layout

```
hermes-group-model-sync/
├── plugin.yaml                     # agent-half manifest (installer-compatible manifest_version 1, api_version 1)
├── __init__.py                     # register(ctx): CLI command + /gms command + bundled skill
├── plugin.py                       # shared core: load, validate, diff, apply, verify, rollback
├── presets/
│   ├── presets.json                # the ONE source of preset truth (both halves read this)
│   └── README.md                   # format, key scope, the absent-key rule
├── dashboard/
│   ├── manifest.json               # publishes the backend router to the dashboard/desktop
│   ├── plugin_api.py               # FastAPI router: /presets, /presets/plan, /presets/apply, /profiles, /status
│   └── config_core.py              # the single host-config write path (atomic, profile-scoped)
├── skills/
│   └── group-model-sync/SKILL.md   # agent-facing how-to for the verbs (read-only intent)
├── desktop/
│   └── plugin.js                   # desktop half: Groups, Agents, Presets tabs
├── test/
│   ├── presets-schema.test.js      # node: catalog schema
│   ├── preset-diff.test.js         # node: diff rules, driven through plugin.py
│   ├── plugin-version.test.js      # node: version + namespace lockstep
│   ├── test_preset_apply.py        # pytest: apply/verify/rollback against a scratch HERMES_HOME
│   ├── test_plugin_api.py          # pytest: the router, profile-scoped
│   ├── resolver.test.js            # live-session resolution
│   ├── apply-member.test.js        # the durable per-member write
│   └── read-default.test.js        # the authoritative profile-default read
└── README.md
```

## Tests

```bash
node test/presets-schema.test.js && node test/preset-diff.test.js && node test/plugin-version.test.js \
  && node test/resolver.test.js && node test/apply-member.test.js && node test/read-default.test.js

# needs the host Hermes package importable (and fastapi/httpx for the API tests):
python -m pytest test/test_preset_apply.py test/test_plugin_api.py
```

`test/preset-diff.test.js` drives the shipped Python diff instead of reimplementing it in JavaScript, so it needs `python3` with PyYAML (`PYTHON=/path/to/python` overrides). The pytest files need an interpreter that can import the Hermes package — the Hermes venv; `HERMES_AGENT_SOURCE` / `HERMES_AGENT_SRC` points them at a source tree if it is not importable.

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
- **Presets (agent half)**: `plugin.py` reads the target profile's own `config.yaml` (never the merged default tree), computes the minimal diff, copies the file to a backup, then writes through the host's own fail-closed atomic chokepoint (`hermes_cli.config.atomic_config_write`) with `set_hermes_home_override(<target profile>)` bound for the duration, and reads every changed key back. The desktop Presets tab reaches the same code through this plugin's own backend namespace (`ctx.rest('/presets')` → `/api/plugins/group-model-sync/presets`), so the two halves cannot drift.

All data is local. No telemetry, no network beyond the app's own gateway.

## Known limitations

- **A per-member model or reasoning change writes that member's profile default.** The Groups-tab Apply calls `profiles.configure {name, model, provider}` on the member's door. Group plumbing sessions follow the member's profile config, so the change applies to every group that member is in, and to that member's default everywhere else. The Agents-tab Sync is intentionally global (it pushes to every session of the profile by design).
- **A preset apply is future-sessions-only, and the CLI has no verb that pins existing ones.** An apply rewrites `config.yaml`; a session that is already running keeps the model it started with. Pinning a profile's existing live sessions is a gateway/desktop operation — the **Desktop → Model Sync → Agents** action — so the CLI half exposes no such verb rather than shipping one that cannot work. There is no state.db write anywhere in this plugin.
- **A profile needs a `config.yaml` before a preset can be applied.** A freshly created profile has none (its identity is `.env` + `SOUL.md`), and the plugin refuses to *create* one — every write must be backed by a backup and a before-hash. Create it first, e.g. `hermes -p <profile> config set model.provider <provider>`.
- **A declared `aux` tier is not applied.** `tiers.aux` is carried and schema-validated, but the target's `auxiliary.*` slots are outside the locked key scope, so plan and receipt report it as "declared, not applied" instead of writing it.
- **Applying a preset rewrites the whole `config.yaml`** through the host's atomic writer, which normalizes formatting and drops comments — the same thing `hermes config set` does. That is what the pre-write backup is for.
- **Publishing to the plugin catalog is not part of this work.** Nothing here is submitted upstream.

## License

MIT
