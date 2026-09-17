"""hermes-group-model-sync — agent half.

Registers, for the profile Hermes is running as:

* the CLI verbs ``hermes group-model-sync …`` (D7 write path, D8 safety bar);
* the read-only in-session slash command ``/gms status`` — an in-session command must
  never rewrite config mid-conversation, so mutations stay on the CLI and the pane;
* the bundled ``group-model-sync`` skill (how-to for the verbs, read-only intent).

No agent tool is registered (D.3 / N2: a model-facing tool that can rewrite config.yaml
is a mis-invocation risk with no upside), and no hook is registered.

The shared core lives in ``plugin.py`` and is loaded lazily so that importing this module
is cheap and side-effect free (an agent-half import happens on every CLI start).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ID = "hermes-group-model-sync"
CORE_MODULE = "hermes_group_model_sync_core"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


def _load_core():
    """Load ``plugin.py`` under a plugin-unique module name.

    By path rather than ``from . import plugin``: the dashboard's route importer
    (``hermes_dashboard_plugin_<id>``) and the agent-plugin package loader must both
    reach the same core module object.
    """
    cached = sys.modules.get(CORE_MODULE)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent / "plugin.py"
    spec = importlib.util.spec_from_file_location(CORE_MODULE, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise RuntimeError(f"cannot load plugin core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_MODULE] = module
    spec.loader.exec_module(module)
    return module


# ── output helpers ──────────────────────────────────────────────────────────


def _fail(message: str, code: int = EXIT_REFUSED) -> int:
    sys.stderr.write(f"group-model-sync: {message}\n")
    return code


def _print_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _fmt(value: Any) -> str:
    if value is None:
        return "(absent)"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


# ── target resolution (I1) ──────────────────────────────────────────────────


class _Usage(Exception):
    pass


def _targets(args) -> List[str]:
    core = _load_core()
    listed = [p.strip() for p in (getattr(args, "profiles", "") or "").split(",") if p.strip()]
    all_local = bool(getattr(args, "all_local", False))
    if listed and all_local:
        raise _Usage("--profiles and --all-local are mutually exclusive")
    if listed:
        return listed
    if all_local:
        resolved = core.local_profiles()
        if not resolved:
            raise _Usage("--all-local resolved no profiles with a config.yaml")
        sys.stdout.write("resolved local profiles: " + ", ".join(resolved) + "\n")
        return resolved
    raise _Usage("no target named: pass --profiles a,b or --all-local (I1: every write names its profile)")


def _preset_or_fail(preset_id: str):
    core = _load_core()
    try:
        return core.get_preset(preset_id)
    except core.PresetError as exc:
        raise _Usage(str(exc)) from exc


# ── verbs ───────────────────────────────────────────────────────────────────


def _verb_preset(args) -> int:
    core = _load_core()
    try:
        catalog = core.load_catalog()
    except core.PresetError as exc:
        return _fail(str(exc), EXIT_FAILED)
    if getattr(args, "preset_command", None) == "show":
        try:
            preset = core.get_preset(args.preset_id)
        except core.PresetError as exc:
            return _fail(str(exc))
        if args.json:
            _print_json(preset)
            return EXIT_OK
        sys.stdout.write(f"{preset['id']} — {preset['name']}\n")
        if preset["description"]:
            sys.stdout.write(f"  {preset['description']}\n")
        sys.stdout.write(f"  guarded: {'yes' if preset['guarded'] else 'no'}\n")
        for key, value in core.preset_key_values(preset):
            sys.stdout.write(f"  {key} = {_fmt(value)}\n")
        for profile in sorted(preset.get("assignments") or {}):
            view = core.profile_view(preset, profile)
            pairs = core.preset_key_values(view)
            sys.stdout.write(f"  [{profile}] " + "; ".join(f"{k} = {_fmt(v)}" for k, v in pairs) + "\n")
        for note in core.declared_not_applied(preset):
            sys.stdout.write(f"  [not applied] {note['tier']}: {note['reason']}\n")
        return EXIT_OK
    if args.json:
        _print_json({"schema": catalog["schema"], "path": catalog["path"],
                     "sha256": catalog["sha256"], "presets": catalog["presets"]})
        return EXIT_OK
    sys.stdout.write(f"{catalog['path']}  (schema {catalog['schema']}, sha256 {catalog['sha256'][:12]}…)\n")
    for preset in catalog["presets"]:
        guarded = " [guarded]" if preset["guarded"] else ""
        sys.stdout.write(f"  {preset['id']:<20} {preset['description'] or preset['name']}{guarded}\n")
    return EXIT_OK


def _plan_lines(plan: Dict[str, Any]) -> List[str]:
    where = plan.get("config_file")
    lines = [f"profile {plan['profile']}" + (f"  ({where})" if where else "")]
    if not plan["ok"]:
        lines.append(f"  REFUSED: {plan['error']}")
        return lines
    if not plan["changes"]:
        lines.append("  no changes — every key this preset names already matches (idempotent)")
    for change in plan["changes"]:
        lines.append(f"  {change['key']}: {_fmt(change['before'])} -> {_fmt(change['after'])}")
    for row in plan["unchanged"]:
        lines.append(f"  unchanged: {row['key']} = {_fmt(row['value'])}")
    for note in plan["declared_not_applied"]:
        lines.append(f"  declared, not applied — {note['tier']}: {note['reason']}")
    return lines


def _verb_plan(args) -> int:
    core = _load_core()
    try:
        preset = _preset_or_fail(args.preset_id)
        targets = _targets(args)
    except _Usage as exc:
        return _fail(str(exc))
    plans = []
    refused = False
    for name in targets:
        try:
            plans.append(core.plan_profile(name, preset))
        except core.PresetError as exc:
            plans.append({"profile": name, "ok": False, "error": str(exc), "changes": [],
                          "unchanged": [], "declared_not_applied": []})
        refused = refused or not plans[-1]["ok"]
    if args.json:
        _print_json({"preset": preset["id"], "read_only": True, "plans": plans})
    else:
        for plan in plans:
            sys.stdout.write("\n".join(_plan_lines(plan)) + "\n")
        sys.stdout.write("read-only: nothing was written\n")
    return EXIT_FAILED if refused else EXIT_OK


def _verify_lines(verify: Dict[str, Any]) -> List[str]:
    lines = []
    for row in verify["keys"]:
        lines.append(f"  {row['key']} = {_fmt(row['value'])}  [{row['status']}]")
    usage = verify["state_db"] or {}
    label = "VERIFIED" if usage.get("verified") else "UNVERIFIED"
    lines.append(f"  state.db: {label} — {usage.get('reason', '')}")
    return lines


def _verb_apply(args) -> int:
    core = _load_core()
    try:
        preset = _preset_or_fail(args.preset_id)
        targets = _targets(args)
    except _Usage as exc:
        return _fail(str(exc))
    if not args.yes:
        # A6 — a real refusal that writes nothing and NAMES --yes.
        plans = []
        for name in targets:
            try:
                plans.append(core.plan_profile(name, preset))
            except core.PresetError as exc:
                plans.append({"profile": name, "ok": False, "error": str(exc), "changes": [],
                              "unchanged": [], "declared_not_applied": []})
        if args.json:
            _print_json({"preset": preset["id"], "applied": False, "dry_run": bool(args.dry_run),
                         "plans": plans})
        else:
            for plan in plans:
                sys.stdout.write("\n".join(_plan_lines(plan)) + "\n")
        sys.stdout.write("dry run: nothing was written — re-run with --yes to apply\n")
        return EXIT_OK if args.dry_run else _fail("refusing to write without --yes", EXIT_REFUSED)

    receipts = []
    ok = True
    for name in targets:
        try:
            receipt = core.apply_profile(name, preset, confirm_expensive=args.confirm_expensive,
                                         verify=args.verify, require_verified=args.require_verified)
        except core.PresetError as exc:
            receipts.append({"profile": name, "ok": False, "error": str(exc), "written": False})
            ok = False
            continue
        receipts.append(receipt)
        ok = ok and bool(receipt.get("ok")) and (receipt.get("written") or receipt.get("no_op"))
    if args.json:
        _print_json({"preset": preset["id"], "applied": True, "receipts": receipts})
        return EXIT_OK if ok else EXIT_FAILED
    for receipt in receipts:
        if receipt.get("error"):
            sys.stdout.write(f"profile {receipt['profile']}: REFUSED — {receipt['error']}\n")
            continue
        state = "no-op (already applied)" if receipt.get("no_op") else "applied"
        sys.stdout.write(f"profile {receipt['profile']}: {state}\n")
        for change in receipt["changes"]:
            sys.stdout.write(f"  {change['key']}: {_fmt(change['before'])} -> {_fmt(change['after'])}\n")
        for row in receipt["readback"]:
            sys.stdout.write(f"  read-back {row['key']} = {_fmt(row['value'])}  [{row['status']}]\n")
        for note in receipt.get("declared_not_applied", []):
            sys.stdout.write(f"  declared, not applied — {note['tier']}: {note['reason']}\n")
        if receipt.get("backup"):
            sys.stdout.write(f"  backup: {receipt['backup']}\n")
        sys.stdout.write(f"  config sha256: {receipt['before_sha256']} -> {receipt['after_sha256']}\n")
        if receipt.get("write_error"):
            sys.stdout.write(f"  WRITE FAILED: {receipt['write_error']}\n")
        if receipt.get("verify"):
            sys.stdout.write("\n".join(_verify_lines(receipt["verify"])) + "\n")
    return EXIT_OK if ok else EXIT_FAILED


def _verb_verify(args) -> int:
    core = _load_core()
    try:
        preset = _preset_or_fail(args.preset_id) if getattr(args, "preset_id", None) else None
        targets = _targets(args)
    except _Usage as exc:
        return _fail(str(exc))
    results = []
    ok = True
    require = bool(getattr(args, "require_verified", False))
    for name in targets:
        try:
            result = core.verify_profile(name, preset)
        except core.PresetError as exc:
            results.append({"profile": name, "ok": False, "verified": False, "error": str(exc),
                            "keys": [], "state_db": None})
            ok = False
            continue
        results.append(result)
        if not result["ok"]:
            ok = False
        if require and not result.get("verified"):
            ok = False
    if args.json:
        _print_json({"ok": ok, "verified": bool(results) and all(r.get("verified") for r in results),
                     "require_verified": require, "results": results})
        return EXIT_OK if ok else EXIT_FAILED
    for result in results:
        if result.get("error"):
            sys.stdout.write(f"profile {result['profile']}: REFUSED — {result['error']}\n")
            continue
        sys.stdout.write(f"profile {result['profile']}  ({result['config_file']})\n")
        sys.stdout.write("\n".join(_verify_lines(result)) + "\n")
    return EXIT_OK if ok else EXIT_FAILED


def _verb_rollback(args) -> int:
    core = _load_core()
    try:
        result = core.rollback_profile(args.profile, args.backup)
    except core.PresetError as exc:
        return _fail(str(exc))
    if args.json:
        _print_json(result)
        return EXIT_OK if result["ok"] else EXIT_FAILED
    sys.stdout.write(f"profile {result['profile']}  ({result['config_file']})\n")
    sys.stdout.write(f"  restored from: {result['from']}\n")
    sys.stdout.write(f"  pre-rollback safety copy: {result['pre_rollback_backup']}\n")
    sys.stdout.write(f"  config sha256: {result['before_sha256']} -> {result['after_sha256']}\n")
    sys.stdout.write(f"  re-verified: {'ok' if result['verified'] else 'MISMATCH'}\n")
    return EXIT_OK if result["ok"] else EXIT_FAILED


def _verb_doctor(args) -> int:
    core = _load_core()
    report = core.doctor_report()
    if args.json:
        _print_json(report)
        return EXIT_OK if report["ok"] else EXIT_FAILED
    sys.stdout.write(f"python: {report['python']}   yaml: {report['yaml']}\n")
    sys.stdout.write(f"active profile: {report['active_profile']}   hermes home: {report['hermes_home']}\n")
    sys.stdout.write(f"profiles root: {report['profiles_root']}\n")
    sys.stdout.write(f"local profiles ({len(report['local_profiles'])}): "
                     f"{', '.join(report['local_profiles']) or 'none'}\n")
    sys.stdout.write(f"presets: {report['presets_file']}  "
                     f"schema {'ok' if report['presets_schema_ok'] else 'INVALID'}  "
                     f"sha256 {report['presets_sha256']}\n")
    for preset in report["presets"]:
        note = f"  [declared, not applied: {', '.join(preset['declared_not_applied'])}]" \
            if preset["declared_not_applied"] else ""
        sys.stdout.write(f"  {preset['id']:<20} guarded={'yes' if preset['guarded'] else 'no'}"
                         f"  keys={len(preset['keys'])}{note}\n")
    sys.stdout.write(f"version drift: plugin.yaml={report['plugin_yaml_version']} "
                     f"desktop={report['desktop_version']} "
                     f"{'DRIFT' if report['version_drift'] else 'in lockstep'}\n")
    for error in report["errors"]:
        sys.stdout.write(f"  ERROR: {error}\n")
    return EXIT_OK if report["ok"] else EXIT_FAILED


# ── slash command: /gms status (read-only, always) ──────────────────────────


def _slash_gms(raw_args: str) -> Optional[str]:
    core = _load_core()
    verb = (raw_args or "").strip().split(" ")[0].lower() or "status"
    if verb != "status":
        return ("gms: only the read-only 'status' verb exists in-session. Presets are applied "
                "with `hermes group-model-sync apply` or from the desktop pane.")
    try:
        state = core.status_for_profile(core.active_profile_name())
    except Exception as exc:
        return f"gms: status unavailable ({exc})"
    if state.get("error"):
        return f"gms: status unavailable ({state['error']})"
    match = ", ".join(state["matching_presets"]) if state["matching_presets"] else "no preset matches"
    return (f"gms status — profile {state['profile']}\n"
            f"  provider:  {state['provider'] or '(unset)'}\n"
            f"  model:     {state['model'] or '(unset)'}\n"
            f"  reasoning: {state['reasoning'] or '(unset)'}\n"
            f"  presets:   {match}\n"
            "  read-only: this command never writes config")


# ── registration ────────────────────────────────────────────────────────────


def _build_cli(sub) -> None:
    """setup_fn: build the ``hermes group-model-sync`` argparse tree.

    The host hands this function the parser for the plugin's own command (it is created
    with ``subparsers.add_parser(name, ...)``), so the verbs live one level below it.
    """
    sub = sub.add_subparsers(dest="verb", metavar="{preset,plan,apply,verify,rollback,doctor}")
    sub.required = False

    preset = sub.add_parser("preset", help="inspect the preset catalog")
    preset_sub = preset.add_subparsers(dest="preset_command", metavar="{list,show}")
    p_list = preset_sub.add_parser("list", help="list preset ids + one-line description")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=_verb_preset)
    p_show = preset_sub.add_parser("show", help="full tier table for one preset")
    p_show.add_argument("preset_id", metavar="<id>")
    p_show.add_argument("--json", action="store_true", help="machine-readable output")
    p_show.set_defaults(func=_verb_preset)

    plan = sub.add_parser("plan", help="read-only diff of one preset against named profiles")
    plan.add_argument("preset_id", metavar="<id>")
    plan.add_argument("--profiles", default="", help="comma-separated profile names")
    plan.add_argument("--all-local", action="store_true", help="every local profile with a config.yaml")
    plan.add_argument("--json", action="store_true", help="machine-readable output")
    plan.set_defaults(func=_verb_plan)

    apply_cmd = sub.add_parser("apply", help="apply one preset to explicitly named profiles")
    apply_cmd.add_argument("preset_id", metavar="<id>")
    apply_cmd.add_argument("--profiles", default="", help="comma-separated profile names")
    apply_cmd.add_argument("--all-local", action="store_true", help="every local profile with a config.yaml")
    apply_cmd.add_argument("--yes", action="store_true", help="required: confirm the write")
    apply_cmd.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    apply_cmd.add_argument("--confirm-expensive", action="store_true",
                           help="confirm a guarded preset (expensive-model round-trip)")
    apply_cmd.add_argument("--verify", action="store_true", help="verify the target's state.db after writing")
    apply_cmd.add_argument("--require-verified", action="store_true",
                           help="treat an UNVERIFIED result as failure (D14)")
    apply_cmd.add_argument("--json", action="store_true", help="machine-readable receipt")
    apply_cmd.set_defaults(func=_verb_apply)

    verify = sub.add_parser("verify", help="read-only read-back of config + state.db")
    verify.add_argument("preset_id", nargs="?", metavar="<id>", default=None)
    verify.add_argument("--profiles", default="", help="comma-separated profile names")
    verify.add_argument("--all-local", action="store_true", help="every local profile with a config.yaml")
    verify.add_argument("--require-verified", action="store_true",
                        help="treat an UNVERIFIED state.db as failure (D14)")
    verify.add_argument("--json", action="store_true", help="machine-readable output")
    verify.set_defaults(func=_verb_verify)

    rollback = sub.add_parser("rollback", help="restore one profile's config.yaml from a backup")
    rollback.add_argument("--profile", required=True, help="profile to restore")
    rollback.add_argument("--from", dest="backup", default=None, help="backup file (default: newest)")
    rollback.add_argument("--json", action="store_true", help="machine-readable output")
    rollback.set_defaults(func=_verb_rollback)

    doctor = sub.add_parser("doctor", help="env, profile resolution, preset schema, version drift")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=_verb_doctor)


def _cli_handler(args) -> int:
    """handler_fn: dispatch a parsed verb, always exiting with a real code."""
    func = getattr(args, "func", None)
    if func is None:
        sys.stderr.write("group-model-sync: no verb given "
                         "(try: preset list, plan, apply, verify, rollback, doctor)\n")
        return EXIT_REFUSED
    try:
        return int(func(args))
    except _Usage as exc:
        return _fail(str(exc))


def register(ctx) -> None:  # noqa: D401 - the host's plugin entry point
    """Hermes agent-plugin entry point (V6)."""
    core = _load_core()
    ctx.register_cli_command(
        "group-model-sync",
        "Fleet-wide provider/model presets: plan, apply, verify, rollback (profile-native)",
        _build_cli,
        _cli_handler,
        description=("Apply a named provider/model preset to explicitly named profiles. "
                     "Every write targets one profile's own config.yaml, with a plan-then-diff, "
                     "a backup and a mandatory read-back. No network, no credential file."),
    )
    ctx.register_command(
        "gms",
        _slash_gms,
        description="group-model-sync status (read-only): resolved provider/model/reasoning + preset match",
        args_hint="status",
    )
    skill_path = Path(__file__).resolve().parent / "skills" / "group-model-sync" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "group-model-sync",
            skill_path,
            description=("How to inspect and apply provider/model presets from the CLI "
                         "(group-model-sync)"),
        )
    _ = core  # the core is loaded eagerly only to fail loudly at registration time
