"""End-to-end tests for the agent half against a REAL scratch HERMES_HOME.

Every test drives the shipped ``plugin.py`` / ``__init__.py`` code against a temporary
Hermes home with real profile directories, a real ``config.yaml`` and a real
``state.db`` — no mocks of the config layer and no writes anywhere near ``~/.hermes``.

Run (the interpreter must be able to import the host package — the Hermes venv is the
supported runner; ``HERMES_AGENT_SRC`` overrides the source-tree path):

    python -m pytest test/test_preset_apply.py

What it pins (leo-design.md D5/D7/D8, invariants I1-I5, D14):
  * apply writes ONLY the keys the preset names, and reads every one of them back;
  * a backup exists before the write, named config.yaml.bak-<UTC>-<preset id>;
  * a second identical apply writes nothing (I3) and says so;
  * plan is read-only: the config is byte-identical afterwards (I4/A4);
  * unparseable config, unknown preset, unnamed/missing profile all refuse;
  * a guarded preset refuses without an explicit confirmation;
  * verify reads the target's state.db, and reports UNVERIFIED (never success) when
    there are no usage rows yet (D14);
  * rollback restores a backup and re-verifies;
  * an aux tier is declared-but-not-applied, and the target's auxiliary block is
    untouched (D10).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

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
core = _load("gms_core_under_test", REPO / "plugin.py")
cli = _load("gms_init_under_test", REPO / "__init__.py")


# ── fixtures ────────────────────────────────────────────────────────────────


BASE_CONFIG = {
    "model": {
        "provider": "nous",
        "default": "placeholder/model",
        "base_url": "",
        "api_mode": "chat_completions",
    },
    "agent": {"reasoning_effort": "low"},
    "fallback_providers": [{"provider": "nous", "model": "placeholder/fallback"}],
    "auxiliary": {"vision": {"provider": "auto", "model": ""}},
    "untouched_custom": {"keep": "me"},
}


def _write_config(path: Path, data) -> None:
    import yaml
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def scratch(tmp_path, monkeypatch):
    """A real Hermes home with two named profiles, isolated from ~/.hermes."""
    home = tmp_path / ".hermes"
    profiles = home / "profiles"
    for name in ("t-alpha", "t-beta"):
        (profiles / name).mkdir(parents=True, exist_ok=True)
        _write_config(profiles / name / "config.yaml", BASE_CONFIG)
    db = profiles / "t-alpha" / "state.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """CREATE TABLE session_model_usage (
               session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
               billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '',
               task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
               input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
               cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
               reasoning_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
               actual_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT, cost_source TEXT,
               first_seen REAL, last_seen REAL,
               PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))"""
    )
    con.commit()
    con.close()

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return {"home": home, "profiles": profiles, "alpha": profiles / "t-alpha", "beta": profiles / "t-beta"}


def _catalog(tmp_path: Path, presets) -> Path:
    path = tmp_path / "fixture-presets.json"
    path.write_text(json.dumps({"schema": 1, "presets": presets}), encoding="utf-8")
    return path


def _preset(catalog_path: Path, preset_id: str):
    return next(p for p in core.load_catalog(catalog_path)["presets"] if p["id"] == preset_id)


GUARDED = {
    "id": "guarded-example",
    "name": "Guarded example",
    "description": "carries a model the gateway may treat as expensive",
    "guarded": True,
    "tiers": {"main": {"provider": "prov-a", "model": "big-model"}, "aux": None, "fallback": None},
}

WITH_AUX = {
    "id": "with-aux",
    "name": "With aux",
    "description": "declares an aux tier the locked key scope cannot express",
    "guarded": False,
    "tiers": {
        "main": {"provider": "prov-a", "model": "model-a"},
        "aux": {"provider": "prov-b", "model": "model-b"},
        "fallback": None,
    },
}


# ── plan is read-only ───────────────────────────────────────────────────────


def test_plan_writes_nothing(scratch):
    preset = core.get_preset("fleet-default")
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)
    plan = core.plan_profile("t-alpha", preset)
    assert plan["ok"] is True
    assert plan["changes"], "a plan against a different route must have changes"
    assert _sha(config) == before, "plan must leave config.yaml byte-identical (I4/A4)"
    assert not list(scratch["alpha"].glob("config.yaml.bak-*")), "plan must not create a backup"


def test_plan_refuses_unparseable_config(scratch):
    config = scratch["alpha"] / "config.yaml"
    config.write_text("model: [unclosed\n  bad: :\n", encoding="utf-8")
    plan = core.plan_profile("t-alpha", core.get_preset("fleet-default"))
    assert plan["ok"] is False
    assert "not parseable" in (plan["error"] or "")


# ── apply ───────────────────────────────────────────────────────────────────


def test_apply_writes_only_named_keys_and_reads_them_back(scratch):
    preset = core.get_preset("fleet-default")
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)

    receipt = core.apply_profile("t-alpha", preset, verify=True)

    assert receipt["ok"] is True, receipt
    assert receipt["written"] is True
    assert receipt["before_sha256"] == before
    assert receipt["after_sha256"] != before
    assert all(row["status"] == "ok" for row in receipt["readback"]), receipt["readback"]

    import yaml
    written = yaml.safe_load(config.read_text(encoding="utf-8"))
    named = dict(core.preset_key_values(preset))
    assert written["model"]["provider"] == named["model.provider"]
    assert written["model"]["default"] == named["model.default"]
    assert written["model"]["base_url"] == named["model.base_url"]
    assert written["model"]["api_mode"] == named["model.api_mode"]
    assert written["agent"]["reasoning_effort"] == named["agent.reasoning_effort"]
    assert written["fallback_providers"] == named["fallback_providers"]

    # I2 — nothing else moved.
    assert written["auxiliary"] == BASE_CONFIG["auxiliary"]
    assert written["untouched_custom"] == BASE_CONFIG["untouched_custom"]

    backup = Path(receipt["backup"])
    assert backup.exists(), "I4: the backup must exist before/after the write"
    assert backup.name.startswith("config.yaml.bak-")
    assert backup.name.endswith("-fleet-default")

    assert receipt["verify"]["state_db"]["status"] in {"verified", "no-usage-rows-yet"}
    assert receipt["declared_not_applied"] == []


def test_second_apply_is_a_noop(scratch):
    preset = core.get_preset("fleet-default")
    config = scratch["alpha"] / "config.yaml"
    first = core.apply_profile("t-alpha", preset)
    assert first["written"] is True
    after_first = _sha(config)

    second = core.apply_profile("t-alpha", preset)
    assert second["no_op"] is True
    assert second["written"] is False
    assert second["changes"] == []
    assert second["backup"] is None
    assert _sha(config) == after_first, "I3: the second apply must not touch the file"
    assert second["ok"] is True


def test_apply_refuses_unknown_preset_and_bad_target(scratch):
    with pytest.raises(core.PresetError):
        core.get_preset("no-such-preset")
    with pytest.raises(core.PresetError):
        core.apply_profile("", core.get_preset("minimal"))
    with pytest.raises(core.PresetError):
        core.apply_profile("not-a-profile", core.get_preset("minimal"))
    with pytest.raises(core.PresetError):
        core.apply_profile("Default", core.get_preset("minimal"))  # 'default' has no config.yaml here


def test_apply_refuses_unparseable_config(scratch):
    config = scratch["alpha"] / "config.yaml"
    config.write_text("model: [unclosed\n", encoding="utf-8")
    before = _sha(config)
    with pytest.raises(core.RefusedError):
        core.apply_profile("t-alpha", core.get_preset("fleet-default"))
    assert _sha(config) == before, "a refused apply must not touch the file"


def test_guarded_preset_requires_explicit_confirmation(scratch, tmp_path):
    catalog = _catalog(tmp_path, [GUARDED])
    preset = _preset(catalog, "guarded-example")
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)

    with pytest.raises(core.RefusedError) as exc:
        core.apply_profile("t-alpha", preset)
    assert "--confirm-expensive" in str(exc.value)
    assert _sha(config) == before

    receipt = core.apply_profile("t-alpha", preset, confirm_expensive=True)
    assert receipt["ok"] is True and receipt["written"] is True
    assert receipt["guarded"] is True


def test_aux_tier_is_declared_not_applied(scratch, tmp_path):
    catalog = _catalog(tmp_path, [WITH_AUX])
    preset = _preset(catalog, "with-aux")
    receipt = core.apply_profile("t-alpha", preset)
    assert receipt["ok"] is True
    assert [row["tier"] for row in receipt["declared_not_applied"]] == ["aux"]

    import yaml
    written = yaml.safe_load((scratch["alpha"] / "config.yaml").read_text(encoding="utf-8"))
    assert written["auxiliary"] == BASE_CONFIG["auxiliary"], "the locked key scope must not reach auxiliary.*"


# ── verify ──────────────────────────────────────────────────────────────────


def test_verify_reads_the_target_state_db(scratch):
    preset = core.get_preset("fleet-default")
    model = preset["tiers"]["main"]["model"]

    rows_before = core.verify_profile("t-alpha", preset)
    assert rows_before["state_db"]["verified"] is False
    assert rows_before["state_db"]["status"] == "no-usage-rows-yet"

    db = scratch["alpha"] / "state.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, billing_base_url, task, api_call_count)"
        " VALUES (?,?,?,?,?,?)",
        ("20260917_000000_aaaaaa", model, "opencode-go", "https://opencode.ai/zen/go/v1", "", 3),
    )
    con.commit()
    con.close()

    rows_after = core.verify_profile("t-alpha", preset)
    assert rows_after["state_db"]["verified"] is True
    assert rows_after["state_db"]["status"] == "verified"
    assert rows_after["state_db"]["api_calls"] == 3
    assert rows_after["state_db"]["rows"] == 1
    # Usage rows exist, but the CONFIG still holds the pre-apply route: a state.db hit
    # alone is never "verified" — the config keys must agree too (I5).
    assert rows_after["ok"] is False
    assert any(row["status"] == "mismatch" for row in rows_after["keys"])

    core.apply_profile("t-alpha", preset)
    applied = core.verify_profile("t-alpha", preset)
    assert applied["ok"] is True, applied["keys"]
    assert applied["state_db"]["verified"] is True


def test_verify_reports_mismatch_before_apply(scratch):
    preset = core.get_preset("fleet-default")
    result = core.verify_profile("t-alpha", preset)
    assert result["ok"] is False
    assert any(row["status"] == "mismatch" for row in result["keys"])


# ── rollback ────────────────────────────────────────────────────────────────


def test_rollback_restores_the_backup(scratch):
    import yaml
    preset = core.get_preset("fleet-default")
    config = scratch["alpha"] / "config.yaml"
    original = yaml.safe_load(config.read_text(encoding="utf-8"))
    core.apply_profile("t-alpha", preset)
    assert yaml.safe_load(config.read_text(encoding="utf-8")) != original

    result = core.rollback_profile("t-alpha")
    assert result["ok"] is True and result["verified"] is True
    assert yaml.safe_load(config.read_text(encoding="utf-8")) == original
    assert Path(result["pre_rollback_backup"]).exists()


# ── CLI wiring (exit codes are an acceptance criterion) ─────────────────────


def _cli_argv(argv):
    top = argparse.ArgumentParser(prog="hermes")
    subs = top.add_subparsers()
    command = subs.add_parser("group-model-sync")
    cli._build_cli(command)
    return top.parse_args(["group-model-sync", *argv])


def test_cli_apply_without_yes_writes_nothing_and_names_yes(scratch, capsys):
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)
    code = cli._cli_handler(_cli_argv(["apply", "fleet-default", "--profiles", "t-alpha"]))
    out = capsys.readouterr()
    assert code != 0, "A6: apply without --yes must exit non-zero"
    assert "--yes" in (out.out + out.err)
    assert _sha(config) == before
    assert not list(scratch["alpha"].glob("config.yaml.bak-*"))


def test_cli_dry_run_exits_zero_and_writes_nothing(scratch, capsys):
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)
    code = cli._cli_handler(_cli_argv(["apply", "fleet-default", "--profiles", "t-alpha", "--dry-run"]))
    capsys.readouterr()
    assert code == 0
    assert _sha(config) == before


def test_cli_apply_with_yes_reports_the_receipt(scratch, capsys):
    code = cli._cli_handler(_cli_argv(["apply", "fleet-default", "--profiles", "t-alpha", "--yes", "--verify", "--json"]))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    receipt = payload["receipts"][0]
    assert receipt["written"] is True
    assert receipt["backup"]
    assert all(row["status"] == "ok" for row in receipt["readback"])


def test_cli_plan_exits_zero_and_writes_nothing(scratch, capsys):
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)
    code = cli._cli_handler(_cli_argv(["plan", "fleet-default", "--profiles", "t-alpha", "--json"]))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["read_only"] is True
    assert payload["plans"][0]["changes"]
    assert _sha(config) == before


def test_cli_refuses_an_implicit_target(scratch, capsys):
    code = cli._cli_handler(_cli_argv(["plan", "fleet-default"]))
    captured = capsys.readouterr()
    assert code != 0
    assert "--profiles" in (captured.out + captured.err)


def test_cli_doctor_reports_lockstep_and_schema(scratch, capsys):
    code = cli._cli_handler(_cli_argv(["doctor", "--json"]))
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["presets_schema_ok"] is True
    assert report["version_drift"] is False
    assert "t-alpha" in report["local_profiles"]


def test_slash_gms_status_is_read_only(scratch):
    config = scratch["alpha"] / "config.yaml"
    before = _sha(config)
    text = cli._slash_gms("status")
    assert "provider" in text and "model" in text
    assert _sha(config) == before


# ── D14: UNVERIFIED is reported, and is a FAILURE only when asked ───────────


def test_verify_exit_code_follows_the_config_not_the_state_db(scratch, capsys):
    # Nothing applied yet -> the config read-back mismatches -> failure.
    code = cli._cli_handler(_cli_argv(["verify", "fleet-default", "--profiles", "t-alpha"]))
    out = capsys.readouterr().out
    assert code != 0, out
    assert "mismatch" in out

    core.apply_profile("t-alpha", core.get_preset("fleet-default"))

    # Config now matches, but this profile's state.db holds no usage rows for the model:
    # D14 says report UNVERIFIED and do NOT fail unless --require-verified is passed.
    code = cli._cli_handler(_cli_argv(["verify", "fleet-default", "--profiles", "t-alpha"]))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "UNVERIFIED" in out

    code = cli._cli_handler(
        _cli_argv(["verify", "fleet-default", "--profiles", "t-alpha", "--require-verified"]))
    out = capsys.readouterr().out
    assert code != 0, out
    assert "UNVERIFIED" in out


def test_verify_json_separates_ok_from_verified(scratch, capsys):
    core.apply_profile("t-alpha", core.get_preset("fleet-default"))
    cli._cli_handler(_cli_argv(["verify", "fleet-default", "--profiles", "t-alpha", "--json"]))
    payload = json.loads(capsys.readouterr().out)
    result = payload["results"][0]
    assert result["ok"] is True, result
    assert result["verified"] is False, result
    assert result["state_db"]["status"] in {"no-usage-rows-yet", "no-state-db"}


def test_apply_require_verified_fails_on_an_unverified_state_db(scratch, capsys):
    core.apply_profile("t-alpha", core.get_preset("fleet-default"))
    receipt = core.apply_profile("t-alpha", core.get_preset("fleet-default"), verify=True,
                                require_verified=True)
    assert receipt["no_op"] is True
    assert receipt["verify"]["verified"] is False
    assert receipt["ok"] is False

    # and the same call without --require-verified is a plain success
    receipt = core.apply_profile("t-alpha", core.get_preset("fleet-default"), verify=True)
    assert receipt["ok"] is True
    assert receipt["verify"]["verified"] is False


# ── the exact Leo ruling delta (B-1, B-4, B-5, B-6, O4, O6) ─────────────────


def _cli_parser():
    """The real argparse tree the host builds for this plugin's command."""
    top = argparse.ArgumentParser(prog="hermes")
    subs = top.add_subparsers()
    command = subs.add_parser("group-model-sync")
    cli._build_cli(command)
    return top, command


def test_one_catalog_and_it_is_the_installed_package_catalog(scratch):
    """B-4 — v1 has one source of preset truth, and it is the package's own file."""
    catalog_path = core.presets_path()
    assert catalog_path == REPO / "presets" / "presets.json"
    catalog = core.load_catalog()
    assert catalog["path"] == str(catalog_path)
    assert {p["id"] for p in catalog["presets"]} >= {"fleet-default", "minimal"}


def test_manifest_ships_no_alternative_catalog_key():
    """B-4 — the unwired config_schema.preset_file declaration is gone, not ignored."""
    manifest = (REPO / "plugin.yaml").read_text(encoding="utf-8")
    assert "preset_file" not in manifest
    # Hermes' config_schema is a mapping of setting name -> {type, ...}, so an empty
    # mapping is the honest "declares no settings" form (a JSON-Schema-shaped block
    # registers a phantom setting called "properties").
    assert "config_schema: {}" in manifest
    assert "additionalProperties" not in manifest
    assert "properties:" not in manifest


def test_shipped_presets_are_labelled_example_only(scratch):
    """O4 — an EXAMPLE preset with machine values is labelled, and the fleet-array
    preset (per-profile assignments) is NOT an example and must not be labelled."""
    machine_valued = 0
    for preset in core.list_presets():
        main = preset["tiers"]["main"] or {}
        if not any(main.get(key) for key in ("provider", "model", "base_url")):
            continue
        machine_valued += 1
        assert "(example)" in preset["name"].lower(), preset["name"]
        assert "EXAMPLE ONLY" in preset["description"], preset["description"]
    assert machine_valued >= 2


def test_shipped_fleet_array_preset_is_real_and_per_profile(scratch):
    """The catalog ships at least one assignments preset with a stated read-date
    (provenance) and no example labelling — data, not an illustrative carry."""
    arrays = [p for p in core.list_presets() if p.get("assignments")]
    assert arrays, "catalog must carry a real per-profile assignments preset"
    for preset in arrays:
        assert preset["tiers"]["main"] is None, "an assignments preset carries no global route"
        assert "(example)" not in preset["name"].lower()
        assert "EXAMPLE ONLY" not in preset["description"]
        import re as _re
        assert _re.search(r"\d{4}-\d{2}-\d{2}", preset["description"]), "read-date provenance required"
        assert len(preset["assignments"]) >= 2
        for profile, tier in preset["assignments"].items():
            assert profile.strip() == profile and profile
            assert tier.get("provider") or tier.get("model"), profile


# ── per-profile assignments ─────────────────────────────────────────────────


ASSIGN = {
    "id": "array-two",
    "name": "Array of two",
    "description": "per-profile routes read 2026-09-17",
    "guarded": False,
    "tiers": {"main": None, "aux": None, "fallback": None},
    "assignments": {
        "t-alpha": {"provider": "prov-a", "model": "model-a", "base_url": "https://a.example/v1",
                    "api_mode": "chat_completions"},
        "t-beta": {"provider": "prov-b", "model": "model-b"},
    },
}


def test_assignments_plan_uses_each_profiles_own_route(scratch, tmp_path):
    preset = _preset(_catalog(tmp_path, [ASSIGN]), "array-two")
    alpha = core.plan_profile("t-alpha", preset)
    beta = core.plan_profile("t-beta", preset)
    assert alpha["ok"] and beta["ok"]
    pairs_alpha = {c["key"]: c["after"] for c in alpha["changes"]}
    assert pairs_alpha["model.provider"] == "prov-a"
    assert pairs_alpha["model.default"] == "model-a"
    assert pairs_alpha["model.base_url"] == "https://a.example/v1"
    assert "agent.reasoning_effort" not in pairs_alpha, "an unnamed key is never in the diff"
    pairs_beta = {c["key"]: c["after"] for c in beta["changes"]}
    assert pairs_beta["model.provider"] == "prov-b"
    assert "model.base_url" not in pairs_beta, "t-beta's tier names no base_url — nothing is written"


def test_assignments_refuse_an_unassigned_profile(scratch, tmp_path):
    """D5 — no fallback to a global route: an unassigned target is refused, not guessed."""
    preset = _preset(_catalog(tmp_path, [ASSIGN]), "array-two")
    (scratch["profiles"] / "t-gamma").mkdir()
    _write_config(scratch["profiles"] / "t-gamma" / "config.yaml", BASE_CONFIG)
    try:
        core.plan_profile("t-gamma", preset)
        raise AssertionError("plan_profile must raise for an unassigned target")
    except core.PresetError as exc:
        assert "does not name profile 't-gamma'" in str(exc)
        assert "t-alpha" in str(exc) and "t-beta" in str(exc), "the refusal lists the assigned names"


def test_assignments_apply_writes_then_idempotent(scratch, tmp_path):
    import yaml
    preset = _preset(_catalog(tmp_path, [ASSIGN]), "array-two")
    receipt = core.apply_profile("t-beta", preset)
    assert receipt["ok"] and receipt["written"] and not receipt["no_op"]
    written = yaml.safe_load((scratch["beta"] / "config.yaml").read_text(encoding="utf-8"))
    assert written["model"]["provider"] == "prov-b" and written["model"]["default"] == "model-b"
    assert written["agent"]["reasoning_effort"] == BASE_CONFIG["agent"]["reasoning_effort"]
    again = core.apply_profile("t-beta", preset)
    assert again["no_op"] and not again["written"]


def test_assignments_rejection_surfaces_through_the_cli(scratch, capsys):
    """The shipped fleet array must refuse a profile it does not assign — via the REAL
    CLI path (its own core instance reads the installed catalog, D4)."""
    (scratch["profiles"] / "t-gamma").mkdir()
    _write_config(scratch["profiles"] / "t-gamma" / "config.yaml", BASE_CONFIG)
    code = cli._cli_handler(_cli_argv(["plan", "config-3", "--profiles", "t-gamma"]))
    out = capsys.readouterr().out
    assert code != 0, "a refused plan must exit non-zero"
    assert "REFUSED" in out and "does not name profile" in out
    assert "(assigned:" in out, "the refusal must list the assigned profile names"


def test_shipped_config3_is_a_real_per_profile_array(scratch):
    """Brief §2: a `config-3` preset with the seven named fleet profiles."""
    catalog = core.load_catalog(REPO / "presets" / "presets.json")
    c3 = next(p for p in catalog["presets"] if p["id"] == "config-3")
    assert c3["guarded"] is True, "a fleet array naming gpt-5.6-luna-900k is guarded"
    assert c3["tiers"]["main"] is None
    assert sorted(c3["assignments"]) == [
        "frida", "hazen", "leo", "mozi", "orda", "proteus", "shaka"]
    routes = {(t.get("provider"), t.get("model")) for t in c3["assignments"].values()}
    assert routes == {
        ("openai-codex", "gpt-5.6-luna-900k"),
        ("opencode-go", "muse-spark-1.3-contributor"),
        ("opencode-go", "deepseek-v4.1-flash"),
    }


def test_verify_and_status_resolve_per_profile(scratch, tmp_path, monkeypatch):
    catalog = _catalog(tmp_path, [ASSIGN])
    monkeypatch.setattr(core, "presets_path", lambda: catalog)
    preset = _preset(catalog, "array-two")
    core.apply_profile("t-alpha", preset)
    verify = core.verify_profile("t-alpha", preset)
    assert verify["ok"] and all(row["status"] == "ok" for row in verify["keys"])
    status = core.status_for_profile("t-alpha")
    assert "array-two" in status["matching_presets"]


def test_doctor_lists_assignments(scratch, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "presets_path", lambda: _catalog(tmp_path, [ASSIGN]))
    report = core.doctor_report()
    row = next(r for r in report["presets"] if r["id"] == "array-two")
    assert row["assignments"] == ["t-alpha", "t-beta"]
    assert row["keys"] == [], "a null global main names no global keys"


def test_sync_sessions_surface_is_fully_removed(scratch, capsys):
    """B-1 — no CLI flag, no help entry, no blocked exit code, no dead reference."""
    top, command = _cli_parser()
    help_text = command.format_help()
    assert "--sync-sessions" not in help_text

    with pytest.raises(SystemExit):
        top.parse_args(["group-model-sync", "apply", "fleet-default",
                        "--profiles", "t-alpha", "--sync-sessions"])
    assert "--sync-sessions" in capsys.readouterr().err

    assert not hasattr(cli, "EXIT_BLOCKED")
    init_source = Path(cli.__file__).read_text(encoding="utf-8")
    for token in ("--sync-sessions", "sync_sessions", "EXIT_BLOCKED", "_sync_sessions_refused"):
        assert token not in init_source, token
    core_source = Path(core.__file__).read_text(encoding="utf-8")
    assert "session_rows" not in core_source, "the dead session-enumeration helper must be gone"
    adapter = (REPO / "dashboard" / "config_core.py").read_text(encoding="utf-8")
    assert "session_rows" not in adapter, "the dead session-enumeration helper must be gone"


def test_apply_has_no_sync_sessions_attribute(scratch):
    """The parsed apply namespace carries no removed field at all."""
    args = _cli_argv(["apply", "fleet-default", "--profiles", "t-alpha", "--yes"])
    assert not hasattr(args, "sync_sessions")


def test_missing_config_yaml_refuses_and_creates_nothing(scratch, capsys):
    """B-5 / I4 — a profile with no config.yaml is refused; nothing is created."""
    bare = scratch["profiles"] / "t-bare"
    bare.mkdir(parents=True, exist_ok=True)
    (bare / ".env").write_text("", encoding="utf-8")
    (bare / "SOUL.md").write_text("# bare profile\n", encoding="utf-8")
    preset = core.get_preset("fleet-default")

    plan_exc = None
    try:
        core.plan_profile("t-bare", preset)
    except core.PresetError as exc:
        plan_exc = exc
    assert plan_exc is not None, "a profile with no config.yaml must be refused, not planned"
    assert "no config.yaml" in str(plan_exc)
    assert "config set" in str(plan_exc)  # names the host remediation

    with pytest.raises(core.PresetError):
        core.apply_profile("t-bare", preset)

    plan_code = cli._cli_handler(_cli_argv(["plan", "fleet-default", "--profiles", "t-bare"]))
    plan_out = capsys.readouterr()
    assert plan_code != 0
    assert "REFUSED" in plan_out.out and "no config.yaml" in plan_out.out, plan_out.out
    apply_code = cli._cli_handler(_cli_argv(["apply", "fleet-default", "--profiles", "t-bare", "--yes"]))
    apply_out = capsys.readouterr()
    assert apply_code != 0
    assert "REFUSED" in apply_out.out, apply_out.out

    assert not (bare / "config.yaml").exists(), "the plugin must never create a first config file"
    assert not list(bare.glob("config.yaml.bak-*")), "no backup may be created"
    assert sorted(p.name for p in bare.iterdir()) == [".env", "SOUL.md"]


def _set_dotted(cfg, dotted, value):
    segments = dotted.split(".")
    node = cfg
    for segment in segments[:-1]:
        node = node.setdefault(segment, {})
    node[segments[-1]] = value


def test_apply_materializes_no_host_defaults_and_preserves_every_other_key(scratch):
    """B-6 / I2 — the target's OWN file is the read source; no merged default tree lands."""
    import yaml
    from hermes_cli.config import DEFAULT_CONFIG

    preset = core.get_preset("fleet-default")
    config = scratch["alpha"] / "config.yaml"
    core.apply_profile("t-alpha", preset)
    written = yaml.safe_load(config.read_text(encoding="utf-8"))

    expected = json.loads(json.dumps(BASE_CONFIG))
    for key, value in core.preset_key_values(preset):
        _set_dotted(expected, key, value)
    assert written == expected, "the written file must be the original file plus the named keys"

    materialized = sorted(k for k in DEFAULT_CONFIG if k not in BASE_CONFIG and k in written)
    assert materialized == [], f"load_config()'s merged defaults must never be written: {materialized}"
    assert set(written) == set(BASE_CONFIG), "the file's own top-level key set must survive exactly"


def test_fallback_providers_round_trips_as_the_whole_list(scratch, tmp_path):
    """O6 — fallback_providers is one list of {provider, model}; the full list round-trips."""
    two_entry = {
        "id": "two-fallback",
        "name": "Two fallbacks (example)",
        "description": "EXAMPLE ONLY",
        "guarded": False,
        "tiers": {
            "main": {"provider": "prov-a", "model": "model-a"},
            "aux": None,
            "fallback": [
                {"provider": "prov-b", "model": "model-b"},
                {"provider": "prov-c", "model": "model-c"},
            ],
        },
    }
    preset = _preset(_catalog(tmp_path, [two_entry]), "two-fallback")
    config = scratch["alpha"] / "config.yaml"
    before = core.load_catalog(REPO / "presets" / "presets.json")  # keep the shipped catalog in scope
    assert before["schema"] == 1
    before_value = BASE_CONFIG["fallback_providers"]

    assert core.preset_key_values(preset).count(("fallback_providers", None)) == 0
    assert dict(core.preset_key_values(preset))["fallback_providers"] == two_entry["tiers"]["fallback"]

    receipt = core.apply_profile("t-alpha", preset)
    assert receipt["ok"] is True and receipt["written"] is True
    import yaml
    written = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert written["fallback_providers"] == two_entry["tiers"]["fallback"]
    assert written["fallback_providers"] != before_value
    row = next(r for r in receipt["readback"] if r["key"] == "fallback_providers")
    assert row["status"] == "ok"
    assert row["value"] == two_entry["tiers"]["fallback"]  # exact list equality, order included
    assert all(set(entry) == {"provider", "model"} for entry in written["fallback_providers"])
