from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app


runner = CliRunner()


def test_upstream_style_cli_exposes_wizard_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "wizard" in result.stdout


def test_upstream_style_cli_dispatches_wizard_with_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot-telegram" / "config.json"
    seen: dict[str, Path] = {}

    def fake_run_wizard(path: Path, io=None):
        seen["path"] = path

    monkeypatch.setattr("nanobot.cli.commands.run_wizard", fake_run_wizard)

    result = runner.invoke(app, ["wizard", "--config", str(config_path)])

    assert result.exit_code == 0
    assert seen["path"] == config_path
