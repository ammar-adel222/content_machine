"""End-to-end CLI tests using Typer's CliRunner in an isolated CM_ROOT."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cm.cli import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"


def test_cli_new_validate_and_status_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    res_new = runner.invoke(
        app,
        ["new", "cache-build", "--title", "Building a Cache", "--domain", "systems", "--json"],
    )
    assert res_new.exit_code == 0, res_new.output
    new_data = json.loads(res_new.output)
    assert new_data["state"]["slug"] == "cache-build"

    # Starter template fails validation due to TODO placeholders
    res_val_fail = runner.invoke(app, ["validate", "cache-build", "--json"])
    assert res_val_fail.exit_code == 1
    fail_report = json.loads(res_val_fail.output)
    assert fail_report["passed"] is False

    # Copy valid example blueprint into a new run and validate
    res_new_ex = runner.invoke(
        app,
        [
            "new",
            "password-system",
            "--from-blueprint",
            str(EXAMPLE_BP),
            "--json",
        ],
    )
    assert res_new_ex.exit_code == 0, res_new_ex.output

    res_val_pass = runner.invoke(app, ["validate", "password-system", "--json"])
    assert res_val_pass.exit_code == 0, res_val_pass.output
    pass_report = json.loads(res_val_pass.output)
    assert pass_report["passed"] is True

    res_status = runner.invoke(app, ["status", "password-system", "--json"])
    assert res_status.exit_code == 0
    status_data = json.loads(res_status.output)
    assert status_data["current_stage"] == "audio"
    assert status_data["stages"]["blueprint"]["status"] == "passed"

    res_list = runner.invoke(app, ["status", "--json"])
    assert res_list.exit_code == 0
    list_data = json.loads(res_list.output)
    assert len(list_data["runs"]) == 2


def test_cli_schema_command() -> None:
    res = runner.invoke(app, ["schema", "blueprint"])
    assert res.exit_code == 0
    schema_obj = json.loads(res.output)
    assert "properties" in schema_obj
    assert "beats" in schema_obj["properties"]


def test_cli_ideas_add_list_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    res_add = runner.invoke(
        app,
        [
            "ideas",
            "add",
            "--title",
            "Building HTTPS From Scratch",
            "--domain",
            "security",
            "--hook",
            "Anyone on Wi-Fi can read your packets unless you lock them.",
            "--failure",
            "Plain HTTP snooping",
            "--failure",
            "Shared secret key interception",
            "--failure",
            "Fake server impersonation",
            "--json",
        ],
    )
    assert res_add.exit_code == 0, res_add.output
    added = json.loads(res_add.output)
    assert added["idea"]["id"] == "idea-001"

    res_list = runner.invoke(app, ["ideas", "list", "--json"])
    assert res_list.exit_code == 0
    assert len(json.loads(res_list.output)["ideas"]) == 1

    res_pick = runner.invoke(app, ["ideas", "pick", "idea-001", "--json"])
    assert res_pick.exit_code == 0, res_pick.output
    picked = json.loads(res_pick.output)
    assert picked["idea"]["status"] == "in_progress"
    assert picked["state"]["idea_id"] == "idea-001"
