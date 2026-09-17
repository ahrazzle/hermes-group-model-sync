# presets/ — the one source of preset truth

`presets.json` is the single source of preset data for both halves of this package (D4):
the agent half reads it from its install folder, the desktop half reads it through the
plugin's own backend route (`GET /presets`). There is no second copy anywhere — not in
`plugin.py`, not in `__init__.py`, not in `desktop/plugin.js`, not in the README — and no
alternative catalog path: `plugin.yaml` declares no `preset_file` (or any other) lookup
key, so this installed file is the only catalog v1 can read.

## What a preset is

A named, versioned group of settings that a human applies deliberately, by name, to
explicitly named profiles:

```json
{
  "schema": 1,
  "presets": [
    {
      "id": "fleet-default",
      "name": "Fleet default (example)",
      "description": "one line shown in the preset list",
      "guarded": false,
      "tiers": {
        "main":     { "provider": "", "model": "", "base_url": null, "api_mode": null,
                      "reasoning_effort": null },
        "aux":      null,
        "fallback": [ { "provider": "", "model": "" } ]
      }
    }
  ]
}
```

`reasoning_effort`, when named, must be one of
`minimal | low | medium | high | xhigh | max | ultra`.

## Per-profile assignments (an optional field on one preset)

A preset may instead carry `assignments`: a mapping of profile name to its own
main-tier values. This expresses a fleet *array* — different profiles, different
routes — as one named preset:

```json
{
  "id": "config-3",
  "name": "Config 3 (live fleet array, read YYYY-MM-DD)",
  "description": "states where and when the array was read",
  "guarded": true,
  "tiers": { "main": null, "aux": null, "fallback": null },
  "assignments": {
    "proteus": { "provider": "...", "model": "..." },
    "orda":    { "provider": "...", "model": "...", "base_url": "...", "api_mode": "..." }
  }
}
```

The rules:

- `tiers.main: null` is legal **only** together with a non-empty `assignments` map.
  An assignments preset has no global fallback route, deliberately.
- Each assigned tier is validated exactly like a main tier (same keys, same enum).
- **A profile the preset does not name is refused**, in plan, apply, verify and the
  pane — with a message listing the assigned names. Presets never guess a route.
- Shared keys (`tiers.fallback`, `declared-not-applied` notes) still work; an
  assignment only replaces the main route for the named profile.

## Which keys a preset may touch

Exactly these six target keys, and nothing else (D10):

| preset field | config.yaml key |
|---|---|
| `tiers.main.provider` | `model.provider` |
| `tiers.main.model` | `model.default` |
| `tiers.main.base_url` | `model.base_url` |
| `tiers.main.api_mode` | `model.api_mode` |
| `tiers.main.reasoning_effort` | `agent.reasoning_effort` |
| `tiers.fallback` | `fallback_providers` (whole list) |

`tiers.aux` is carried and schema-validated but **not applied** by v1: the target's
auxiliary slots (`auxiliary.<slot>.provider|model`) are outside the locked key scope, so
a declared aux tier is reported as "declared, not applied" in every plan and receipt
instead of being written silently.

## The rules that make presets safe

- **Absent is not null, and neither means "clear it".** A field a preset does not name —
  or names as `null` — is never written. There is no way for a preset to blank a key in
  v1. Use a non-empty string to set a value.
- **Plan first, always.** `hermes group-model-sync plan <id> --profiles a,b` is read-only
  and prints the exact keys that would change.
- **Only differing keys are written.** A preset applied to a profile that already matches
  it produces zero writes and a receipt that says so; applying the same preset twice in a
  row is a no-op on the second run.
- **Every write is backed up and read back.** The current `config.yaml` is copied to
  `config.yaml.bak-<UTC>-<preset id>` beside it before anything is written, and the keys
  are read back from the file afterwards. A write that does not read back as intended is
  reported as a failure. `hermes group-model-sync rollback --profile p` restores the
  newest backup and re-verifies.
- **Config only, future sessions only.** A preset changes `config.yaml`; sessions that
  are already running keep the model they started with. Pinning existing sessions is a
  separate, explicit desktop action (Model Sync → Agents) — the CLI half deliberately has
  no verb for it — and it is never a side effect of an apply.
- **The route moves as a unit.** `model.provider` and `model.default` alone are a half
  switch: `model.base_url` and `model.api_mode` would still point at the previous
  endpoint. Presets that change provider should name all four, as both shipped examples
  do.

## The shipped presets

`fleet-default` and `minimal` are labelled **EXAMPLE ONLY**. They carry illustrative
machine-specific values (an opencode-go route and a direct-deepseek route) so that `plan`
has something real to diff against on a first run. They are **not** fleet defaults, **not**
a recommendation, and the machine-specific provider/model/url/fallback values may be stale
or invalid on a fresh install. Replace every value with your own before applying — the
shipped example list is a format demonstration, nothing more.

`config-3` is different: it is a **real per-profile array**, the seven-profile selection
read from a live fleet on 2026-09-17, expressed with `assignments` (see above). It is
`guarded` because one of its routes names a model the gateway may treat as expensive, it
deliberately does not touch `agent.reasoning_effort` or `fallback_providers`, and it is
refused for any profile outside its seven. Fork this file and replace `config-3` with your
own array if you want the same shape for your fleet.

To keep your own values out of a public file, add your presets to your own copy of this
repository before installing it, or paste them into `presets.json` in your installed copy
(`<hermes home>/plugins/hermes-group-model-sync/presets/presets.json`) — but note that an
upgrade replaces the install folder, so keep your copy in git.

## Formatting note

Applying a preset rewrites `config.yaml` through the host's atomic writer, which
normalizes formatting and drops comments (the same thing `hermes config set` does). That
is exactly why the pre-write backup exists.
