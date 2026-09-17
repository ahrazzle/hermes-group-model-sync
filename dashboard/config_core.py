"""Host-config adapter for hermes-group-model-sync — the ONE write path (D7).

Everything the plugin does to a profile's config.yaml goes through this thin module:
the CLI verbs, the in-session status command and the FastAPI routes all call
:func:`write_config`, so there is exactly one place where a write can happen and
exactly one place to audit.

Write path (D7): ``set_hermes_home_override(<target profile dir>)`` ->
``hermes_cli.config.atomic_config_write`` (the host's fail-closed chokepoint —
``require_readable_config_before_write`` first, then a temp file + fsync + atomic
replace) -> ``reset_hermes_home_override``.

The override binds the host's own profile-scoped write path to the TARGET profile for
the duration of the call. Without it the write would inherit whatever HERMES_HOME the
calling process happens to hold, which is exactly the "wrote through the running
profile" failure D3 forbids.

No credential file is opened here: config.yaml is the only path this module writes
(I7), and every read of another profile's state.db is read-only (``mode=ro``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - broken host install
    yaml = None  # type: ignore

__all__ = [
    "CoreError",
    "hermes_home",
    "profiles_root",
    "active_profile_name",
    "resolve_target",
    "list_local_profiles",
    "read_config",
    "write_config",
    "list_backups",
    "model_usage",
]


class CoreError(RuntimeError):
    """Host-layer refusal or environment problem, with a user-facing message."""


def _profiles_module():
    from hermes_cli import profiles as profiles_mod
    return profiles_mod


def hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def profiles_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root()) / "profiles"


def active_profile_name() -> str:
    try:
        return _profiles_module().get_active_profile_name()
    except Exception:
        return "default"


def resolve_target(profile: str) -> Tuple[str, Path, Path]:
    """``(canonical name, profile home, config.yaml path)`` for ONE named profile (I1).

    Refuses an empty name, an invalid name, a non-existent profile, and a profile whose
    config.yaml is missing — the write is what needs the file, and the safety bar
    (D8) is explicit that an unreadable target is never written over.
    """
    name = str(profile or "").strip()
    if not name:
        raise CoreError("no profile named — one profile per apply, named explicitly (I1)")
    profiles_mod = _profiles_module()
    try:
        canon = profiles_mod.normalize_profile_name(name)
        profiles_mod.validate_profile_name(canon)
    except ValueError as exc:
        raise CoreError(str(exc)) from exc
    home = Path(profiles_mod.get_profile_dir(canon))
    if canon != "default" and not profiles_mod.profile_exists(canon):
        raise CoreError(f"profile {canon!r} does not exist ({home})")
    if not home.is_dir():
        raise CoreError(f"profile {canon!r} does not exist ({home})")
    config_path = home / "config.yaml"
    if not config_path.exists():
        raise CoreError(
            f"profile {canon!r} has no config.yaml at {config_path} — refusing to create one "
            "(I4: every write needs a backup and a before-hash). Create it with the host first, "
            f"e.g. `hermes -p {canon} config set model.provider <provider>`.")
    return canon, home, config_path


def list_local_profiles() -> List[str]:
    """Every local profile that actually has a config.yaml, sorted; 'default' first."""
    names: List[str] = []
    profiles_mod = _profiles_module()
    try:
        infos = profiles_mod.list_profiles()
    except Exception as exc:  # pragma: no cover - broken host install
        raise CoreError(f"profile enumeration failed: {exc}") from exc
    for info in infos:
        name = getattr(info, "name", None) or str(info)
        try:
            home = Path(profiles_mod.get_profile_dir(name))
        except Exception:
            continue
        if (home / "config.yaml").exists():
            names.append(name)
    return sorted(names, key=lambda n: (n != "default", n))


def read_config(config_path: Path) -> Tuple[Dict[str, Any], bytes]:
    """The file's OWN mapping and bytes. Fail closed on unreadable/unparseable (D8).

    Deliberately *not* ``load_config()``: that returns DEFAULT_CONFIG merged with the
    file, and writing a merged tree back would materialize every default into the
    user's config.yaml — a mutation far outside the locked key scope (I2).
    """
    path = Path(config_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise CoreError(f"config not found: {path}") from exc
    except OSError as exc:
        raise CoreError(f"config unreadable: {path} ({exc})") from exc
    if yaml is None:  # pragma: no cover - broken host install
        raise CoreError("PyYAML is unavailable in this interpreter — the host install is broken")
    try:
        parsed = yaml.safe_load(raw.decode("utf-8"))
    except Exception as exc:
        raise CoreError(f"config is not parseable YAML: {path} ({exc})") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise CoreError(f"config root is not a mapping: {path}")
    return parsed, raw


def write_config(config_path: Path, data: Dict[str, Any]) -> None:
    """The single write chokepoint: profile-scoped, atomic, fail-closed (D7)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import config as config_mod

    path = Path(config_path)
    token = set_hermes_home_override(path.parent)
    try:
        config_mod.atomic_config_write(path, data)
    finally:
        reset_hermes_home_override(token)


def list_backups(config_path: Path) -> List[Path]:
    """Candidates for ``rollback``: shipped ``config.yaml.bak-*`` files, newest first."""
    parent = Path(config_path).parent
    found = sorted(parent.glob("config.yaml.bak-*"), key=lambda p: p.name, reverse=True)
    return [p for p in found if p.is_file()]


def model_usage(home: Path, model: str) -> Dict[str, Any]:
    """Read-only verification against the target's state.db (D8).

    ``verified`` is true only when the declared model actually has usage rows. A
    missing state.db or no matching rows reports UNVERIFIED with a stated reason —
    never success (D14).
    """
    out: Dict[str, Any] = {
        "db": str(Path(home) / "state.db"),
        "model": model,
        "rows": 0,
        "api_calls": 0,
        "verified": False,
        "status": "unverified",
        "reason": "",
    }
    if not model:
        out["status"] = "no-declared-model"
        out["reason"] = "the preset names no main model to look for"
        return out
    db = Path(home) / "state.db"
    if not db.exists():
        out["status"] = "no-state-db"
        out["reason"] = f"no usage rows yet — {db} does not exist"
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        out["status"] = "state-db-unreadable"
        out["reason"] = f"{db} could not be opened read-only ({exc})"
        return out
    try:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(api_call_count), 0) FROM session_model_usage WHERE model = ?",
            (model,),
        ).fetchone()
    except sqlite3.Error as exc:
        out["status"] = "state-db-unreadable"
        out["reason"] = f"session_model_usage query failed ({exc})"
        return out
    finally:
        con.close()
    rows = int(row[0] or 0)
    calls = int(row[1] or 0)
    out["rows"], out["api_calls"] = rows, calls
    if rows:
        out["verified"] = True
        out["status"] = "verified"
        out["reason"] = f"{rows} usage row(s), {calls} api call(s) recorded for {model}"
    else:
        out["status"] = "no-usage-rows-yet"
        out["reason"] = f"no usage rows yet for {model} in {db}"
    return out
