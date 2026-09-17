"""hermes-group-model-sync — agent-half core.

Preset catalog load + schema validation, profile resolution, effective-config read,
plan (diff), apply, verify and rollback. Shared by the CLI verbs
(``hermes group-model-sync …``), the in-session ``/gms`` status command and the
plugin's FastAPI router in ``dashboard/plugin_api.py``.

Import discipline (I10 / D13): the standard library plus PyYAML — which every Hermes
install already imports — and nothing else. The host config layer is reached only
through ``dashboard/config_core.py`` (one write path, D7), loaded lazily so this
module stays importable on a bare interpreter.

Invariants encoded here (§4 of the locked design — violating one is a failed build):
  I1  one profile per write, named explicitly; the caller prints the resolved list;
  I2  absent != null != "clear it" — a key the preset does not name is never written;
  I3  applying the same preset twice writes nothing the second time (and says so);
  I4  no write without a parseable current config, a backup file and a before-hash;
  I5  a successful write is not success — the read-back is;
  I6  no network call in plan/apply/verify/rollback;
  I7  the target's config.yaml is the only file ever written;
  I8  nothing in the plugin writes to its own install tree.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # PyYAML is a host dependency (D13). Guarded so this module still *imports*
    import yaml  # on a bare interpreter and reports the gap loudly at load time.
except Exception:  # pragma: no cover - only on a broken install
    yaml = None  # type: ignore

__all__ = [
    "PresetError",
    "RefusedError",
    "PRESETS_SCHEMA",
    "REASONING_EFFORT_VALUES",
    "TARGETED_KEYS",
    "presets_path",
    "plugin_root",
    "load_catalog",
    "list_presets",
    "get_preset",
    "plan_profile",
    "apply_profile",
    "verify_profile",
    "rollback_profile",
    "status_for_profile",
    "doctor_report",
]

PLUGIN_ID = "hermes-group-model-sync"
DESKTOP_ID = "group-model-sync"
PRESETS_SCHEMA = 1
REASONING_EFFORT_VALUES = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# The locked key scope (D10). NOTHING outside this tuple is ever written.
KEY_PROVIDER = "model.provider"
KEY_MODEL = "model.default"
KEY_BASE_URL = "model.base_url"
KEY_API_MODE = "model.api_mode"
KEY_REASONING = "agent.reasoning_effort"
KEY_FALLBACK = "fallback_providers"
TARGETED_KEYS = (KEY_PROVIDER, KEY_MODEL, KEY_BASE_URL, KEY_API_MODE, KEY_REASONING, KEY_FALLBACK)

# tiers.main field -> targeted config key. ``aux`` has no row on purpose: the locked
# v1 key scope has no ``auxiliary.*`` slot, so an aux tier is carried, validated and
# reported, never written (see declared_not_applied below).
_MAIN_FIELD_KEYS = (
    ("provider", KEY_PROVIDER),
    ("model", KEY_MODEL),
    ("base_url", KEY_BASE_URL),
    ("api_mode", KEY_API_MODE),
    ("reasoning_effort", KEY_REASONING),
)
_PRESET_KEYS = ("id", "name", "description", "tiers", "guarded", "assignments")
_TIER_KEYS = ("main", "aux", "fallback")
_MAIN_KEYS = ("provider", "model", "base_url", "api_mode", "reasoning_effort")
_FALLBACK_ENTRY_KEYS = ("provider", "model")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BACKUP_RE = re.compile(r"^config\.yaml\.bak-(\d{8}T\d{6}Z)-(.+)$")
_MISSING = object()


class PresetError(Exception):
    """User-facing failure (bad preset id, unreadable catalog, ...)."""


class RefusedError(PresetError):
    """A write was refused by the safety bar (D8). Never a partial write."""


# ── locations ───────────────────────────────────────────────────────────────


def plugin_root() -> Path:
    """The plugin's own install directory. Read-only, always (I8)."""
    return Path(__file__).resolve().parent


def presets_path() -> Path:
    return plugin_root() / "presets" / "presets.json"


def _config_core():
    """``dashboard/config_core.py`` — the single host-config adapter (D7).

    Loaded by path under a plugin-unique module name so it resolves identically
    whether this file was imported by the agent-plugin package loader or by the
    dashboard's ``hermes_dashboard_plugin_<id>`` route importer.
    """
    name = "hermes_group_model_sync_config_core"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = plugin_root() / "dashboard" / "config_core.py"
    if not path.exists():
        raise PresetError(f"host adapter missing: {path}")
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise PresetError(f"cannot load host adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── preset catalog ──────────────────────────────────────────────────────────


def _require_yaml():
    if yaml is None:  # pragma: no cover - broken install
        raise PresetError("PyYAML is unavailable in this interpreter — the host install is broken")


def _unknown_keys(mapping: Dict[str, Any], allowed: Tuple[str, ...], where: str) -> None:
    extra = sorted(k for k in mapping if k not in allowed)
    if extra:
        raise PresetError(f"{where}: unknown key(s) {', '.join(extra)}")


def _require_nonempty_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PresetError(f"{where}: must be a non-empty string")
    return value.strip()


def _optional_str(value: Any, where: str) -> Optional[str]:
    """``None``/absent means 'not named' (I2). An empty string is neither — refused."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PresetError(
            f"{where}: must be a non-empty string or null "
            "(null/absent = leave the key untouched; there is no 'clear it' value in v1)")
    return value.strip()


def _validate_main(tier: Dict[str, Any], where: str) -> Dict[str, Any]:
    _unknown_keys(tier, _MAIN_KEYS, where)
    out: Dict[str, Any] = {}
    if "provider" not in tier and "model" not in tier:
        raise PresetError(f"{where}: must name at least one of provider/model")
    out["provider"] = _optional_str(tier.get("provider"), f"{where}.provider")
    out["model"] = _optional_str(tier.get("model"), f"{where}.model")
    out["base_url"] = _optional_str(tier.get("base_url"), f"{where}.base_url")
    out["api_mode"] = _optional_str(tier.get("api_mode"), f"{where}.api_mode")
    effort = _optional_str(tier.get("reasoning_effort"), f"{where}.reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORT_VALUES:
        raise PresetError(
            f"{where}.reasoning_effort: {effort!r} is not one of {', '.join(REASONING_EFFORT_VALUES)}")
    out["reasoning_effort"] = effort
    return out


def _validate_preset(raw: Any, index: int) -> Dict[str, Any]:
    where = f"presets[{index}]"
    if not isinstance(raw, dict):
        raise PresetError(f"{where}: must be a mapping")
    _unknown_keys(raw, _PRESET_KEYS, where)
    pid = _require_nonempty_str(raw.get("id"), f"{where}.id")
    if not _ID_RE.match(pid):
        raise PresetError(f"{where}.id: {pid!r} must match [a-z0-9][a-z0-9-]*")
    where = f"preset {pid!r}"
    tiers = raw.get("tiers")
    if not isinstance(tiers, dict):
        raise PresetError(f"{where}.tiers: required mapping")
    _unknown_keys(tiers, _TIER_KEYS, f"{where}.tiers")
    # Per-profile assignments (additive, optional): a preset may carry a distinct
    # main-tier per named profile instead of one global route. When assignments is
    # present, tiers.main may be null — and then ONLY assigned profiles are planable,
    # never a fallback route (D5: no inference; an unassigned target is refused).
    assignments = None
    if raw.get("assignments") is not None:
        asg_raw = raw["assignments"]
        if not isinstance(asg_raw, dict) or not asg_raw:
            raise PresetError(f"{where}.assignments: must be a non-empty mapping of "
                              "profile name -> main-tier values (or absent)")
        assignments = {}
        for prof, tier_raw in asg_raw.items():
            pwhere = f"{where}.assignments[{prof!r}]"
            pkey = _require_nonempty_str(prof, f"{where}.assignments: profile key")
            if not isinstance(tier_raw, dict):
                raise PresetError(f"{pwhere}: must be a mapping")
            assignments[pkey] = _validate_main(tier_raw, pwhere)
    main_raw = tiers.get("main")
    if main_raw is None and assignments is not None:
        main = None
    elif not isinstance(main_raw, dict):
        raise PresetError(f"{where}.tiers.main: required mapping "
                          "(or name per-profile assignments instead)")
    else:
        main = _validate_main(main_raw, f"{where}.tiers.main")
    aux = None
    if tiers.get("aux") is not None:
        aux_raw = tiers.get("aux")
        if not isinstance(aux_raw, dict):
            raise PresetError(f"{where}.tiers.aux: must be a mapping or null")
        aux = _validate_main(aux_raw, f"{where}.tiers.aux")
    fallback = None
    if tiers.get("fallback") is not None:
        fb_raw = tiers.get("fallback")
        if not isinstance(fb_raw, list) or not fb_raw:
            raise PresetError(f"{where}.tiers.fallback: must be a non-empty list or null")
        fallback = []
        for i, entry in enumerate(fb_raw):
            ewhere = f"{where}.tiers.fallback[{i}]"
            if not isinstance(entry, dict):
                raise PresetError(f"{ewhere}: must be a mapping")
            _unknown_keys(entry, _FALLBACK_ENTRY_KEYS, ewhere)
            fallback.append({
                "provider": _require_nonempty_str(entry.get("provider"), f"{ewhere}.provider"),
                "model": _require_nonempty_str(entry.get("model"), f"{ewhere}.model"),
            })
    return {
        "id": pid,
        "name": _require_nonempty_str(raw.get("name"), f"{where}.name"),
        "description": str(raw.get("description") or "").strip(),
        "guarded": bool(raw.get("guarded")),
        "tiers": {"main": main, "aux": aux, "fallback": fallback},
        "assignments": assignments,
    }


def load_catalog(path: Optional[Path] = None) -> Dict[str, Any]:
    """Parse + validate the preset catalog. Fails closed (D14): never invents a default."""
    _require_yaml()
    target = Path(path) if path is not None else presets_path()
    try:
        raw_bytes = target.read_bytes()
    except FileNotFoundError as exc:
        raise PresetError(f"preset catalog not found: {target}") from exc
    except OSError as exc:
        raise PresetError(f"preset catalog unreadable: {target} ({exc})") from exc
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise PresetError(f"preset catalog is not valid JSON/YAML: {target} ({exc})") from exc
    if not isinstance(data, dict):
        raise PresetError(f"preset catalog must be a mapping: {target}")
    unknown = sorted(k for k in data if k not in ("schema", "presets"))
    if unknown:
        raise PresetError(f"{target}: unknown top-level key(s) {', '.join(unknown)}")
    schema = data.get("schema")
    if schema != PRESETS_SCHEMA:
        raise PresetError(f"{target}: schema {schema!r} is not supported (expected {PRESETS_SCHEMA})")
    presets_raw = data.get("presets")
    if not isinstance(presets_raw, list) or not presets_raw:
        raise PresetError(f"{target}: 'presets' must be a non-empty list")
    presets = [_validate_preset(item, i) for i, item in enumerate(presets_raw)]
    seen: Dict[str, int] = {}
    for preset in presets:
        if preset["id"] in seen:
            raise PresetError(f"{target}: duplicate preset id {preset['id']!r}")
        seen[preset["id"]] = 1
    reported = {k: v for k, v in data.items() if k not in ("presets",)}
    reported["presets"] = presets
    reported["path"] = str(target)
    reported["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    return reported


def list_presets() -> List[Dict[str, Any]]:
    return load_catalog()["presets"]


def local_profiles() -> List[str]:
    """Local profile names that have a config.yaml (the ``--all-local`` resolver, I1)."""
    return _config_core().list_local_profiles()


def active_profile_name() -> str:
    """The profile this process/profile-scope is serving (the routes' default target)."""
    return _config_core().active_profile_name()


def get_preset(preset_id: str) -> Dict[str, Any]:
    for preset in list_presets():
        if preset["id"] == preset_id:
            return preset
    known = ", ".join(p["id"] for p in list_presets())
    raise PresetError(f"unknown preset {preset_id!r} (known: {known})")


def profile_view(preset: Dict[str, Any], profile: str) -> Dict[str, Any]:
    """The effective one-route view of ``preset`` for ONE canonical profile.

    A preset without ``assignments`` is its own view (today's behavior, byte for
    byte). A preset with ``assignments`` resolves the named profile's tier as the
    main route — and REFUSES any profile it does not name: there is no fallback
    route to a global main (D5: no inference, an unassigned target is refused).
    """
    assignments = preset.get("assignments")
    if not assignments:
        return preset
    tier = assignments.get(str(profile or "").strip())
    if tier is None:
        raise PresetError(
            f"preset {preset['id']!r} assigns per-profile tiers and does not name "
            f"profile {profile!r} (assigned: {', '.join(sorted(assignments))}) — "
            "refusing rather than guessing a route")
    return {**preset, "tiers": {**preset["tiers"], "main": dict(tier)}}


# ── dotted-key helpers ──────────────────────────────────────────────────────


def _get_key(cfg: Dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


def _set_key(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    segs = dotted.split(".")
    cur = cfg
    for seg in segs[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    cur[segs[-1]] = value


def _named(key: str, value: Any) -> bool:
    """True when the preset NAMES this key (I2): absent and null both mean 'not named'."""
    return value is not None


def preset_key_values(preset: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """(targeted config key, new value) for every key this preset NAMES, in scope order.

    Pass a per-profile view (see :func:`profile_view`) for presets that carry
    ``assignments`` — a null main tier contributes no pair here.
    """
    main = preset["tiers"]["main"] or {}
    pairs: Dict[str, Any] = {}
    for field, key in _MAIN_FIELD_KEYS:
        if _named(key, main.get(field)):
            pairs[key] = main[field]
    fallback = preset["tiers"]["fallback"]
    if fallback:
        pairs[KEY_FALLBACK] = [dict(entry) for entry in fallback]
    return [(key, pairs[key]) for key in TARGETED_KEYS if key in pairs]


def declared_not_applied(preset: Dict[str, Any]) -> List[Dict[str, str]]:
    """Tiers the preset declares that the locked v1 key scope cannot express.

    Reported, never silently dropped: an aux tier targets the profile's
    ``auxiliary.*`` slots, which are deliberately outside the targeted key set (D10).
    """
    out: List[Dict[str, str]] = []
    if preset["tiers"]["aux"] is not None:
        out.append({
            "tier": "aux",
            "reason": ("declared aux tier is carried and validated but not written: the locked v1 "
                       "target key scope has no auxiliary.* slot (D10)"),
        })
    return out


def _values_equal(before: Any, after: Any) -> bool:
    if before is _MISSING or after is _MISSING:
        return False
    return before == after


def diff_preset(cfg: Dict[str, Any], preset: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal diff: only keys the preset NAMES, and only when the value differs.

    Pure and side-effect free; ``desktop/plugin.js`` mirrors it for the tab's
    optimistic preview and ``test/preset-diff.test.js`` asserts the two agree.
    """
    changes: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    for key, after in preset_key_values(preset):
        before = _get_key(cfg, key)
        if _values_equal(before, after):
            unchanged.append({"key": key, "value": None if before is _MISSING else before})
        else:
            changes.append({
                "key": key,
                "before": None if before is _MISSING else before,
                "after": after,
            })
    return {"changes": changes, "unchanged": unchanged}


# ── plan / apply / verify / rollback ────────────────────────────────────────


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve(profile: str) -> Tuple[str, Path, Path]:
    """Host-layer profile resolution, surfaced as a plugin-level refusal."""
    core = _config_core()
    try:
        return core.resolve_target(profile)
    except core.CoreError as exc:
        raise PresetError(str(exc)) from exc


def _read_target_config(config_path: Path) -> Tuple[Dict[str, Any], bytes]:
    """Read the target's OWN config.yaml (never the merged default tree) and fail closed."""
    core = _config_core()
    try:
        return core.read_config(config_path)
    except core.CoreError as exc:
        raise PresetError(str(exc)) from exc


def plan_profile(profile: str, preset: Dict[str, Any]) -> Dict[str, Any]:
    """Read-only plan for one explicitly named profile (I1)."""
    core = _config_core()
    canon, home, config_path = _resolve(profile)
    # Per-profile tier resolution BEFORE any read: an unassigned target of an
    # assignments preset raises (callers already surface PresetError as refusals).
    preset = profile_view(preset, canon)
    report: Dict[str, Any] = {
        "profile": canon,
        "profile_home": str(home),
        "config_file": str(config_path),
        "exists": True,
        "before_sha256": None,
        "changes": [],
        "unchanged": [],
        "declared_not_applied": declared_not_applied(preset),
        "guarded": bool(preset.get("guarded")),
        "ok": False,
        "error": None,
    }
    try:
        cfg, raw = _read_target_config(config_path)
    except PresetError as exc:
        report["error"] = str(exc)
        return report
    report["before_sha256"] = _sha256_bytes(raw)
    report.update(diff_preset(cfg, preset))
    report["ok"] = True
    return report


def _backup_path(config_path: Path, preset_id: str, now: Optional[float] = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return config_path.with_name(f"config.yaml.bak-{stamp}-{preset_id}")


def apply_profile(
    profile: str,
    preset: Dict[str, Any],
    *,
    confirm_expensive: bool = False,
    verify: bool = False,
    require_verified: bool = False,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Apply one preset to ONE named profile (I1). Plan -> backup -> write -> read-back."""
    if preset.get("guarded") and not confirm_expensive:
        raise RefusedError(
            f"preset {preset['id']!r} is guarded: it names a model the gateway may treat as "
            "expensive. Re-run with --confirm-expensive (CLI) or confirm in the desktop dialog.")
    plan = plan_profile(profile, preset)
    if not plan["ok"]:
        raise RefusedError(plan["error"] or "plan failed")
    core = _config_core()
    receipt: Dict[str, Any] = {
        "profile": plan["profile"],
        "config_file": plan["config_file"],
        "preset": preset["id"],
        "guarded": bool(preset.get("guarded")),
        "changes": plan["changes"],
        "unchanged": plan["unchanged"],
        "declared_not_applied": plan["declared_not_applied"],
        "before_sha256": plan["before_sha256"],
        "after_sha256": plan["before_sha256"],
        "backup": None,
        "written": False,
        "no_op": not plan["changes"],
        "readback": [],
        "write_error": None,
        "verify": None,
        "ok": True,
        "error": None,
    }
    if not plan["changes"]:
        # I3 — a second identical apply writes nothing and the receipt says so.
        receipt["readback"] = [
            {"key": row["key"], "value": row["value"], "status": "unchanged"}
            for row in plan["unchanged"]
        ]
        if verify:
            receipt["verify"] = verify_profile(plan["profile"], preset)
            if require_verified and not receipt["verify"]["verified"]:
                receipt["ok"] = False
        return receipt

    config_path = Path(plan["config_file"])
    cfg, raw = _read_target_config(config_path)
    backup = _backup_path(config_path, preset["id"], now=now)
    try:
        backup.write_bytes(raw)  # I4 — a backup exists before anything is written
    except OSError as exc:
        raise RefusedError(f"cannot write backup {backup}: {exc}") from exc
    receipt["backup"] = str(backup)

    new_cfg = deepcopy(cfg)
    for change in plan["changes"]:
        _set_key(new_cfg, change["key"], change["after"])
    try:
        core.write_config(config_path, new_cfg)  # D7 — the host's atomic write chokepoint
    except Exception as exc:  # fail closed: the backup is the recovery path
        receipt["ok"] = False
        receipt["write_error"] = f"{type(exc).__name__}: {exc}"
        return receipt

    _, after_raw = _read_target_config(config_path)  # I5 — the read-back is the success
    receipt["after_sha256"] = _sha256_bytes(after_raw)
    after_cfg, _ = _read_target_config(config_path)
    readback: List[Dict[str, Any]] = []
    all_ok = True
    for change in plan["changes"]:
        got = _get_key(after_cfg, change["key"])
        status = "ok" if _values_equal(got, change["after"]) else "mismatch"
        all_ok = all_ok and status == "ok"
        readback.append({"key": change["key"], "value": None if got is _MISSING else got, "status": status})
    receipt["written"] = True
    receipt["readback"] = readback
    receipt["ok"] = all_ok
    if verify:
        receipt["verify"] = verify_profile(plan["profile"], preset)
        if require_verified and not receipt["verify"]["verified"]:
            receipt["ok"] = False
    return receipt


def verify_profile(profile: str, preset: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read-back only: the target's config.yaml, plus its state.db usage rows (D8).

    Two verdicts, deliberately separate:
      ``ok``       — the config read-back: every expected key matches (I5 makes THIS fatal);
      ``verified`` — ``ok`` AND the declared model has usage rows in the target's state.db.
    D14: an unverifiable state.db ("no usage rows yet", "no state.db") is reported as
    UNVERIFIED, never as success, and it is NOT a failure unless the caller asks for
    ``--require-verified``.
    """
    core = _config_core()
    canon, home, config_path = _resolve(profile)
    preset = profile_view(preset, canon) if preset is not None else None
    out: Dict[str, Any] = {
        "profile": canon,
        "config_file": str(config_path),
        "config_sha256": None,
        "keys": [],
        "state_db": None,
        "ok": False,
        "verified": False,
        "error": None,
    }
    try:
        cfg, raw = _read_target_config(config_path)
    except PresetError as exc:
        out["error"] = str(exc)
        return out
    out["config_sha256"] = _sha256_bytes(raw)
    wanted = preset_key_values(preset) if preset is not None else []
    if preset is None:
        wanted = [(k, _get_key(cfg, k)) for k in TARGETED_KEYS if _get_key(cfg, k) is not _MISSING]
    for key, expected in wanted:
        got = _get_key(cfg, key)
        out["keys"].append({
            "key": key,
            "value": None if got is _MISSING else got,
            "expected": expected,
            "status": "ok" if _values_equal(got, expected) else "mismatch",
        })
    config_ok = all(row["status"] == "ok" for row in out["keys"])
    out["state_db"] = core.model_usage(home, _declared_model(preset))
    out["ok"] = config_ok
    out["verified"] = config_ok and bool(out["state_db"].get("verified"))
    return out


def _declared_model(preset: Optional[Dict[str, Any]]) -> str:
    if not preset:
        return ""
    return str((preset["tiers"].get("main") or {}).get("model") or "")


def rollback_profile(profile: str, backup: Optional[str] = None) -> Dict[str, Any]:
    """Restore the target's config.yaml from a named backup and re-verify (D12)."""
    core = _config_core()
    canon, home, config_path = _resolve(profile)
    candidates = core.list_backups(config_path) if backup is None else [Path(backup).expanduser()]
    if not candidates:
        raise RefusedError(f"no config.yaml.bak-* backup found beside {config_path}")
    source = Path(candidates[0])
    if not source.exists():
        raise RefusedError(f"backup not found: {source}")
    source_bytes = source.read_bytes()
    _require_yaml()
    try:
        restored = yaml.safe_load(source_bytes.decode("utf-8"))
    except Exception as exc:
        raise RefusedError(f"backup {source} is not valid YAML: {exc}") from exc
    if not isinstance(restored, dict) or not restored:
        raise RefusedError(f"backup {source} does not hold a config mapping")
    before_sha = _sha256_bytes(config_path.read_bytes()) if config_path.exists() else None
    safety = config_path.with_name(_backup_path(config_path, "pre-rollback").name)
    if config_path.exists():
        safety.write_bytes(config_path.read_bytes())
    core.write_config(config_path, restored)
    _, after_raw = _read_target_config(config_path)
    after_cfg, _ = _read_target_config(config_path)
    match = all(_values_equal(_get_key(after_cfg, k), _get_key(restored, k))
                for k in set(list(restored.keys())))
    return {
        "profile": canon,
        "config_file": str(config_path),
        "from": str(source),
        "pre_rollback_backup": str(safety),
        "before_sha256": before_sha,
        "after_sha256": _sha256_bytes(after_raw),
        "restored_sha256": _sha256_bytes(source_bytes),
        "ok": bool(match),
        "verified": bool(match),
    }


def status_for_profile(profile: str) -> Dict[str, Any]:
    """Read-only status: the profile's configured provider/model/reasoning + preset match."""
    core = _config_core()
    canon, home, config_path = _resolve(profile)
    out: Dict[str, Any] = {"profile": canon, "config_file": str(config_path), "provider": "",
                           "model": "", "reasoning": "", "matching_presets": [], "error": None}
    try:
        cfg, _ = _read_target_config(config_path)
    except PresetError as exc:
        out["error"] = str(exc)
        return out
    for key, field in ((KEY_PROVIDER, "provider"), (KEY_MODEL, "model"), (KEY_REASONING, "reasoning")):
        value = _get_key(cfg, key)
        out[field] = "" if value is _MISSING else value
    try:
        for preset in list_presets():
            try:
                view = profile_view(preset, canon)
            except PresetError:
                continue  # assignments preset that does not name this profile
            pairs = dict(preset_key_values(view))
            if not pairs:
                continue
            if all(_values_equal(_get_key(cfg, k), v) for k, v in pairs.items()):
                out["matching_presets"].append(preset["id"])
    except PresetError as exc:
        out["error"] = str(exc)
    return out


def doctor_report() -> Dict[str, Any]:
    """Env, profile resolution, preset schema, and plugin.yaml <-> desktop version drift."""
    core = _config_core()
    out: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "yaml": getattr(yaml, "__version__", None),
        "hermes_home": str(core.hermes_home()),
        "profiles_root": str(core.profiles_root()),
        "active_profile": core.active_profile_name(),
        "local_profiles": [],
        "presets_file": str(presets_path()),
        "presets_sha256": None,
        "presets_schema_ok": False,
        "presets": [],
        "plugin_yaml_version": None,
        "desktop_version": None,
        "version_drift": None,
        "ok": False,
        "errors": [],
    }
    try:
        out["local_profiles"] = core.list_local_profiles()
    except Exception as exc:
        out["errors"].append(f"profile enumeration failed: {exc}")
    try:
        catalog = load_catalog()
        out["presets_sha256"] = catalog["sha256"]
        out["presets_schema_ok"] = True
        out["presets"] = [{"id": p["id"], "guarded": p["guarded"],
                           "keys": [k for k, _ in preset_key_values(p)],
                           "assignments": sorted(p["assignments"]) if p.get("assignments") else None,
                           "declared_not_applied": [d["tier"] for d in declared_not_applied(p)]}
                          for p in catalog["presets"]]
    except PresetError as exc:
        out["errors"].append(str(exc))
    try:
        _require_yaml()
        manifest = yaml.safe_load((plugin_root() / "plugin.yaml").read_text(encoding="utf-8"))
        out["plugin_yaml_version"] = str(manifest.get("version") or "")
    except Exception as exc:
        out["errors"].append(f"plugin.yaml unreadable: {exc}")
    try:
        source = (plugin_root() / "desktop" / "plugin.js").read_text(encoding="utf-8")
        match = re.search(r"^\s*version:\s*'([^']+)'", source, re.MULTILINE)
        out["desktop_version"] = match.group(1) if match else ""
    except Exception as exc:
        out["errors"].append(f"desktop/plugin.js unreadable: {exc}")
    out["version_drift"] = (out["plugin_yaml_version"] != out["desktop_version"])
    out["ok"] = bool(out["presets_schema_ok"] and not out["version_drift"] and not out["errors"])
    return out


# ── test / verification seam ────────────────────────────────────────────────


def _cli(argv: List[str]) -> int:
    """``python plugin.py diff --current <file.json|yaml> --preset <id>`` — reads stdin JSON.

    Kept for the node parity test (``test/preset-diff.test.js``) and for evidence
    runs: it exercises the SAME ``diff_preset`` the CLI and the routes use. Not
    registered with Hermes — it is not a surface.
    """
    if not argv or argv[0] != "diff":
        print("usage: plugin.py diff --preset <id> [--presets <file>] < current-config.json", file=sys.stderr)
        return 2
    args = dict(zip(argv[1::2], argv[2::2])) if len(argv) > 1 else {}
    preset_id = args.get("--preset")
    if not preset_id:
        print("plugin.py diff: --preset is required", file=sys.stderr)
        return 2
    try:
        catalog = load_catalog(Path(args["--presets"]) if args.get("--presets") else None)
        preset = next(p for p in catalog["presets"] if p["id"] == preset_id)
        current = json.loads(sys.stdin.read() or "{}")
    except StopIteration:
        print(f"plugin.py diff: unknown preset {preset_id!r}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"plugin.py diff: {exc}", file=sys.stderr)
        return 2
    if not isinstance(current, dict):
        print("plugin.py diff: current config must be a JSON object", file=sys.stderr)
        return 2
    print(json.dumps(diff_preset(current, preset), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the node parity test
    sys.exit(_cli(sys.argv[1:]))
