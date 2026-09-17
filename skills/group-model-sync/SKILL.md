---
name: group-model-sync
description: "Use when inspecting or applying fleet provider/model presets for named profiles. Covers hermes group-model-sync plan/apply/verify/rollback and the read-only /gms status command."
version: 1.0.0
author: ahrazzle
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [models, presets, fleet, config, cli]
---

# group-model-sync — applying provider/model presets

A preset is a named group of provider/model/reasoning settings. Applying one writes six
target keys into ONE named profile's own `config.yaml` — and nothing else:

`model.provider`, `model.default`, `model.base_url`, `model.api_mode`,
`agent.reasoning_effort`, `fallback_providers`.

The `fleet-default` and `minimal` presets in `presets/presets.json` are **EXAMPLE ONLY**:
machine-specific values carried so `plan` has something real to diff. They are not fleet
defaults and not a recommendation — replace their values with your own before applying one.
A preset carrying `assignments` (like the shipped `config-3`) is a real per-profile fleet
array: it resolves each named profile's own route and REFUSES any profile it does not
assign — never pick an assignments preset for a target the user named that the array does
not list; report the refusal instead.

## The verbs

```bash
hermes group-model-sync preset list                       # ids + one-line descriptions
hermes group-model-sync preset show <id>                  # every key that preset names
hermes group-model-sync plan   <id> --profiles a,b        # READ-ONLY diff; writes nothing
hermes group-model-sync apply  <id> --profiles a,b --yes [--verify]
hermes group-model-sync verify --profiles a,b             # read-back only (config + state.db)
hermes group-model-sync rollback --profile p [--from <backup>]
hermes group-model-sync doctor                            # env, profiles, schema, drift
```

Add `--json` to any verb for a machine-readable receipt.

## Rules to follow when you are the one driving this

1. **Always `plan` first.** It is read-only, it names every key that would change with
   before → after, and it exits 0 having written nothing. Show the plan to the user.
2. **`apply` needs an explicit target AND `--yes`.** Without `--yes` nothing is written.
   `apply` also refuses an unknown preset id, a profile that does not exist, a profile with
   no `config.yaml`, and an unparseable `config.yaml` — fail closed, never write over a
   broken file.
3. **Presets are a human act.** Never apply a preset as a side effect of another task, and
   never apply one to a profile the user did not name. `--all-local` is only for a case
   where the user explicitly asked for every local profile.
4. **A preset cannot clear a key.** A field the preset does not name (or names as `null`)
   is never written. Applied twice, the second run writes nothing.
5. **Future sessions only.** The apply changes `config.yaml`; a session that is already
   running keeps the model it started with. There is deliberately no CLI verb that pins
   existing sessions: that is a separate, explicit desktop action (Model Sync → Agents),
   never a side effect of an apply.
6. **The read-back is the success.** `--verify` re-reads the target's config and asks its
   `state.db` for the declared model's usage rows; `UNVERIFIED` ("no usage rows yet") is
   reported as such and is not success.
7. **Every write is reversible.** Before writing, the current file is copied to
   `config.yaml.bak-<UTC>-<preset id>`; `rollback --profile p` restores the newest one and
   re-verifies.

## In-session

`/gms status` is read-only and always available: it prints the current profile's resolved
provider, model, reasoning level, and which presets (if any) that combination matches.
There is no in-session command that writes config — an in-session command must never
rewrite config mid-conversation.

## When not to use this skill

- One profile, one session, one model pick → the app's own model picker (`/model`).
- Per-member model selection inside a Bot Mode group chat → the desktop pane's Groups tab.
- Anything that needs a live session pinned right now → Model Sync → Agents (desktop).
