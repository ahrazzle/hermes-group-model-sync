"""HTTP-surface tests for the agent half's backend router (dashboard/plugin_api.py).

These exercise the REAL FastAPI router the desktop pane talks to through ``ctx.rest``
(mounted at ``/api/plugins/<id>/``), against a real scratch HERMES_HOME. The router is
loaded exactly the way the dashboard loads it — importlib by path — so the module's
own path-derived core lookup is covered too.

Run (needs the host package plus fastapi/httpx — the Hermes venv is the supported runner):

    python -m pytest test/test_plugin_api.py

What it pins (leo-design.md D9, D14, I1):
  * the routes are profile-scoped BY THE REQUEST: an explicit target is required for
    plan/apply, and omitting one is a refusal, never a default write;
  * apply refuses without the explicit ``yes`` confirmation and hands back the plan
    instead of writing;
  * the receipt shape the pane renders (changes, read-back rows, backup, verify) is
    returned by the real route;
  * an unknown preset is a 404, a missing target a 400, a missing confirmation a 409;
  * the request models are CLOSED: a field this version does not accept (the removed
    sync-sessions surface in particular) is a 422 at the boundary, never accepted and
    ignored, and no 501 path remains (B-1).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="the router tests need the host's FastAPI")
pytest.importorskip("httpx", reason="fastapi.testclient needs httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _ensure_host_importable() -> None:
    try:
        import hermes_cli  # noqa: F401
        return
    except ImportError:
        src = os.environ.get("HERMES_AGENT_SRC") or str(Path.home() / ".hermes" / "hermes-agent")
        if src not in sys.path:
            sys.path.insert(0, src)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ensure_host_importable()
router_module = _load("gms_router_under_test", REPO / "dashboard" / "plugin_api.py")

PREFIX = "/api/plugins/group-model-sync"
BASE_CONFIG = {
    "model": {"provider": "nous", "default": "placeholder/model", "base_url": "", "api_mode": "chat_completions"},
    "agent": {"reasoning_effort": "low"},
    "fallback_providers": [{"provider": "nous", "model": "placeholder/fallback"}],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import yaml

    home = tmp_path / ".hermes"
    profiles = home / "profiles"
    for name in ("r-alpha", "r-beta"):
        (profiles / name).mkdir(parents=True, exist_ok=True)
        (profiles / name / "config.yaml").write_text(
            yaml.safe_dump(BASE_CONFIG, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    app = FastAPI()
    app.include_router(router_module.router, prefix=PREFIX)
    return {"client": TestClient(app), "profiles": profiles, "alpha": profiles / "r-alpha"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_presets_route_returns_the_catalog(client):
    res = client["client"].get(f"{PREFIX}/presets")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["schema"] == 1
    ids = [p["id"] for p in body["presets"]]
    assert "fleet-default" in ids and "minimal" in ids
    assert all("keys" in p and "declared_not_applied" in p for p in body["presets"])
    for preset in body["presets"]:
        for row in preset["keys"]:
            assert row["key"] in {
                "model.provider", "model.default", "model.base_url", "model.api_mode",
                "agent.reasoning_effort", "fallback_providers",
            }


def test_profiles_route_lists_local_profiles(client):
    res = client["client"].get(f"{PREFIX}/profiles")
    assert res.status_code == 200, res.text
    body = res.json()
    assert sorted(body["profiles"]) == ["r-alpha", "r-beta"]
    assert body["active"] == "custom" or isinstance(body["active"], str)


def test_plan_is_profile_scoped_and_read_only(client):
    config = client["alpha"] / "config.yaml"
    before = _sha(config)
    res = client["client"].post(f"{PREFIX}/presets/plan", json={"preset": "fleet-default", "profiles": ["r-alpha"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["read_only"] is True
    assert [p["profile"] for p in body["plans"]] == ["r-alpha"]
    plan = body["plans"][0]
    assert plan["ok"] is True
    assert plan["changes"], "the fixture route differs from the preset"
    assert all({"key", "before", "after"} <= set(c) for c in plan["changes"])
    assert _sha(config) == before, "a plan must never write"


def test_plan_refuses_an_implicit_target(client):
    res = client["client"].post(f"{PREFIX}/presets/plan", json={"preset": "fleet-default"})
    assert res.status_code == 400
    assert "explicit" in res.json()["detail"] or "profile" in res.json()["detail"]
    assert not list(client["alpha"].glob("config.yaml.bak-*"))


def test_plan_unknown_preset_is_404(client):
    res = client["client"].post(f"{PREFIX}/presets/plan", json={"preset": "nope", "profiles": ["r-alpha"]})
    assert res.status_code == 404


def test_presets_route_carries_assignments_for_the_pane(client):
    """The pane renders one assignments row per preset from this route (D4: one file)."""
    body = client["client"].get(f"{PREFIX}/presets").json()
    arrays = [p for p in body["presets"] if p.get("assignments")]
    assert arrays, "the shipped catalog must expose an assignments preset to the pane"
    for preset in arrays:
        assert preset["keys"] == [], "a null global main names no global keys"
        assert isinstance(preset["assignments"], dict) and len(preset["assignments"]) >= 2


def test_plan_refuses_an_unassigned_target_of_the_shipped_array(client):
    """D5 through the REAL route: an assignments preset refuses profiles it does not
    name — per-target refusal row, not a 500, and nothing written."""
    config = client["alpha"] / "config.yaml"
    before = _sha(config)
    body = client["client"].get(f"{PREFIX}/presets").json()
    array_id = next(p["id"] for p in body["presets"] if p.get("assignments"))
    row = next(p for p in body["presets"] if p["id"] == array_id)
    assigned = set(row["assignments"])
    target = "r-alpha" if "r-alpha" not in assigned else "r-beta"
    res = client["client"].post(f"{PREFIX}/presets/plan", json={"preset": array_id, "profiles": [target]})
    assert res.status_code == 200, res.text
    plan = res.json()["plans"][0]
    assert plan["ok"] is False
    assert "does not name profile" in plan["error"] and "(assigned:" in plan["error"]
    assert _sha(config) == before, "a refusal must never write"


def test_apply_refuses_without_confirmation_and_writes_nothing(client):
    config = client["alpha"] / "config.yaml"
    before = _sha(config)
    res = client["client"].post(f"{PREFIX}/presets/apply", json={"preset": "fleet-default", "profiles": ["r-alpha"]})
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["applied"] is False
    assert detail["plans"][0]["changes"]
    assert _sha(config) == before
    assert not list(client["alpha"].glob("config.yaml.bak-*"))


def test_apply_refuses_an_implicit_target(client):
    res = client["client"].post(f"{PREFIX}/presets/apply", json={"preset": "fleet-default", "yes": True})
    assert res.status_code == 400


def test_apply_returns_the_receipt_the_pane_renders(client):
    res = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "fleet-default", "profiles": ["r-alpha"], "yes": True, "verify": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["applied"] is True and body["ok"] is True
    receipt = body["receipts"][0]
    assert receipt["profile"] == "r-alpha"
    assert receipt["written"] is True and receipt["no_op"] is False
    assert receipt["changes"] and receipt["readback"]
    assert all(row["status"] == "ok" for row in receipt["readback"])
    assert Path(receipt["backup"]).exists()
    # These fixture profiles have no state.db at all: D14 says that is reported as its
    # own stated condition, never as success.
    assert receipt["verify"]["state_db"]["status"] in {
        "verified", "no-usage-rows-yet", "no-state-db"}
    assert receipt["verify"]["state_db"]["verified"] is False

    second = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "fleet-default", "profiles": ["r-alpha"], "yes": True})
    again = second.json()["receipts"][0]
    assert again["no_op"] is True and again["written"] is False and again["changes"] == []


def test_apply_two_explicit_targets_each_get_their_own_receipt(client):
    res = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "minimal", "profiles": ["r-alpha", "r-beta"], "yes": True})
    assert res.status_code == 200, res.text
    receipts = res.json()["receipts"]
    assert [r["profile"] for r in receipts] == ["r-alpha", "r-beta"]
    assert all(r["written"] for r in receipts)


def test_apply_unknown_profile_is_refused_per_target(client):
    res = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "minimal", "profiles": ["r-alpha", "ghost-profile"], "yes": True})
    assert res.status_code == 200
    receipts = {r["profile"]: r for r in res.json()["receipts"]}
    assert receipts["r-alpha"]["written"] is True
    assert receipts["ghost-profile"]["ok"] is False
    assert "does not exist" in receipts["ghost-profile"]["error"]


def test_status_route_is_read_only(client):
    config = client["alpha"] / "config.yaml"
    before = _sha(config)
    res = client["client"].get(f"{PREFIX}/status", params={"profile": "r-alpha"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["profile"] == "r-alpha"
    assert body["provider"] == "nous"
    assert body["model"] == "placeholder/model"
    assert _sha(config) == before


# ── the removed sync-sessions surface (B-1) ─────────────────────────────────


def test_apply_request_rejects_the_removed_sync_field(client):
    """A removed field is refused at the model boundary, never accepted-and-ignored."""
    res = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "fleet-default", "profiles": ["r-alpha"], "yes": True, "sync_sessions": True})
    assert res.status_code == 422, res.text
    assert "sync_sessions" in json.dumps(res.json()["detail"]), res.text
    assert _sha(client["alpha"] / "config.yaml")  # the file is still there and untouched
    assert not list(client["alpha"].glob("config.yaml.bak-*")), "a refused request must not write"


def test_plan_request_rejects_the_removed_sync_field(client):
    res = client["client"].post(
        f"{PREFIX}/presets/plan",
        json={"preset": "fleet-default", "profiles": ["r-alpha"], "sync_sessions": True})
    assert res.status_code == 422, res.text
    assert "sync_sessions" in json.dumps(res.json()["detail"])


def test_unknown_request_fields_are_refused_not_ignored(client):
    """extra="forbid" is the property that keeps a removed key removed."""
    res = client["client"].post(
        f"{PREFIX}/presets/plan",
        json={"preset": "fleet-default", "profiles": ["r-alpha"], "not_a_field": 1})
    assert res.status_code == 422, res.text


def test_the_api_contract_no_longer_names_the_sync_surface(client):
    """No request field and no 501 branch: the surface is gone, not stubbed."""
    spec = client["client"].get("/openapi.json").json()
    assert "sync_sessions" not in json.dumps(spec)
    source = Path(router_module.__file__).read_text(encoding="utf-8")
    assert "sync_sessions" not in source
    assert "501" not in source


def test_apply_happy_path_never_returns_501(client):
    res = client["client"].post(
        f"{PREFIX}/presets/apply",
        json={"preset": "minimal", "profiles": ["r-beta"], "yes": True})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
