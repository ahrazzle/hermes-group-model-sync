"""hermes-group-model-sync — dashboard/desktop backend routes (V5).

Mounted at ``/api/plugins/group-model-sync/`` by ``hermes_cli.web_server_dashboard``
(discovery reads ``dashboard/manifest.json``; the Python half must be in
``plugins.enabled`` before the router is imported at all). The desktop half reaches
these routes through its own ``ctx.rest('/presets')`` door, which is path-scoped to
``/api/plugins/<plugin id>`` by construction — a plugin cannot address another
plugin's API or a core route through it.

Routes (the locked surface names; all profile-scoped by the request):

    GET  /presets         the preset catalog (schema + sha256 + declared-not-applied)
    POST /presets/plan    read-only plan for explicitly named profiles
    POST /presets/apply   apply, with the safety bar (explicit target + yes)
    GET  /profiles        local profiles that have a config.yaml + the active profile
    GET  /status          read-only status for one profile (default: the serving profile)

Read-only except ``apply``. No new gateway RPC, no core edit, no monkeypatching: every
route is a thin wrapper over the same ``plugin.py`` core the CLI uses, so the two halves
cannot drift (D9).

Request bodies are CLOSED (``extra="forbid"``): a field this version does not accept is a
422 at the model boundary, never a silently ignored key. That is what keeps a removed
field removed — there is no accept-and-ignore path left to reach a dead branch.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()

CORE_MODULE = "hermes_group_model_sync_core"


def _core():
    """Load the agent-half core by path (this module is imported standalone by the dashboard)."""
    cached = sys.modules.get(CORE_MODULE)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent.parent / "plugin.py"
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"plugin core missing: {path}")
    spec = importlib.util.spec_from_file_location(CORE_MODULE, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - broken install
        raise HTTPException(status_code=500, detail=f"cannot load plugin core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_MODULE] = module
    spec.loader.exec_module(module)
    return module


class PlanRequest(BaseModel):
    """A read-only plan. An explicit target is required (I1)."""

    model_config = ConfigDict(extra="forbid")

    preset: str = Field(..., description="preset id from GET /presets")
    profiles: List[str] = Field(default_factory=list, description="explicit profile names")
    all_local: bool = Field(False, description="every local profile with a config.yaml")


class ApplyRequest(PlanRequest):
    """An apply. ``yes`` is the explicit confirmation; without it the route refuses."""

    yes: bool = False
    confirm_expensive: bool = False
    verify: bool = False
    dry_run: bool = False


def _fail(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _targets(payload: PlanRequest, core) -> List[str]:
    listed = [p.strip() for p in (payload.profiles or []) if str(p).strip()]
    if listed and payload.all_local:
        raise _fail(400, "--profiles and --all-local are mutually exclusive")
    if listed:
        return listed
    if payload.all_local:
        resolved = core.local_profiles()
        if not resolved:
            raise _fail(400, "all_local resolved no profiles with a config.yaml")
        return resolved
    raise _fail(400, "no target named: pass profiles[] or all_local (I1: every write names its profile)")


def _preset(core, preset_id: str):
    try:
        return core.get_preset(preset_id)
    except core.PresetError as exc:
        raise _fail(404, str(exc)) from exc


@router.get("/presets")
def get_presets() -> Dict[str, Any]:
    """The single source of preset truth (D4) — one file, read by both halves."""
    core = _core()
    try:
        catalog = core.load_catalog()
    except core.PresetError as exc:
        raise _fail(503, str(exc)) from exc
    presets = []
    for preset in catalog["presets"]:
        row = dict(preset)
        row["keys"] = [{"key": key, "value": value} for key, value in core.preset_key_values(preset)]
        row["declared_not_applied"] = core.declared_not_applied(preset)
        presets.append(row)
    return {
        "schema": catalog["schema"],
        "path": catalog["path"],
        "sha256": catalog["sha256"],
        "presets": presets,
    }


@router.post("/presets/plan")
def plan_presets(payload: PlanRequest) -> Dict[str, Any]:
    core = _core()
    preset = _preset(core, payload.preset)
    plans = []
    for name in _targets(payload, core):
        try:
            plans.append(core.plan_profile(name, preset))
        except core.PresetError as exc:
            plans.append({"profile": name, "ok": False, "error": str(exc), "changes": [],
                          "unchanged": [], "declared_not_applied": []})
    return {"preset": preset["id"], "read_only": True, "plans": plans}


@router.post("/presets/apply")
def apply_presets(payload: ApplyRequest) -> Dict[str, Any]:
    core = _core()
    preset = _preset(core, payload.preset)
    targets = _targets(payload, core)
    if not payload.yes:
        # The desktop's two-step confirm: plan first, then resend with yes=true.
        plans = []
        for name in targets:
            try:
                plans.append(core.plan_profile(name, preset))
            except core.PresetError as exc:
                plans.append({"profile": name, "ok": False, "error": str(exc), "changes": [],
                              "unchanged": [], "declared_not_applied": []})
        detail = {"preset": preset["id"], "applied": False, "dry_run": True, "plans": plans,
                  "message": "not applied: resend with yes=true to confirm"}
        if payload.dry_run:
            return detail
        raise HTTPException(status_code=409, detail=detail)
    receipts = []
    ok = True
    for name in targets:
        try:
            receipt = core.apply_profile(name, preset,
                                         confirm_expensive=payload.confirm_expensive,
                                         verify=payload.verify)
        except core.PresetError as exc:
            receipts.append({"profile": name, "ok": False, "error": str(exc), "written": False})
            ok = False
            continue
        receipts.append(receipt)
        ok = ok and bool(receipt.get("ok"))
    return {"preset": preset["id"], "applied": True, "ok": ok, "receipts": receipts}


@router.get("/profiles")
def get_profiles() -> Dict[str, Any]:
    core = _core()
    try:
        names = core.local_profiles()
    except Exception as exc:
        raise _fail(503, f"profile enumeration failed: {exc}") from exc
    return {"active": core.active_profile_name(), "profiles": names}


@router.get("/status")
def get_status(profile: Optional[str] = Query(None, description="profile (default: serving profile)")) -> Dict[str, Any]:
    core = _core()
    target = (profile or "").strip() or core.active_profile_name()
    try:
        state = core.status_for_profile(target)
    except core.PresetError as exc:
        raise _fail(404, str(exc)) from exc
    return state
