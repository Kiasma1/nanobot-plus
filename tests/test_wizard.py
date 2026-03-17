from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.cli.wizard import WizardIO, main as wizard_main, run_wizard, suggest_split_config_path


class ScriptedIO(WizardIO):
    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = iter(responses or [])
        self.messages: list[str] = []

    def write(self, message: str = "") -> None:
        self.messages.append(message)

    def prompt(self, message: str) -> str:
        self.messages.append(message)
        return next(self._responses, "")

    def prompt_secret(self, message: str) -> str:
        self.messages.append(message)
        return next(self._responses, "")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def telegram_config(token: str = "telegram-old-token") -> dict:
    return {
        "agents": {
            "defaults": {
                "workspace": "/tmp/nanobot/workspace",
                "model": "anthropic/claude-opus-4-5",
            }
        },
        "channels": {
            "sendProgress": True,
            "sendToolHints": False,
            "telegram": {
                "enabled": True,
                "token": token,
                "allowFrom": [],
                "proxy": None,
                "replyToMessage": False,
                "groupPolicy": "mention",
            }
        },
        "gateway": {
            "host": "0.0.0.0",
            "port": 18790,
            "heartbeat": {
                "enabled": True,
                "intervalS": 1800,
            },
        },
        "tools": {
            "web": {
                "search": {
                    "apiKey": "",
                    "maxResults": 5,
                }
            },
            "exec": {"timeout": 60, "pathAppend": ""},
            "restrictToWorkspace": True,
            "mcpServers": {},
        },
    }


def keep_existing_responses() -> list[str]:
    return [""] * 6


def test_existing_telegram_keep_unchanged(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    io = ScriptedIO(keep_existing_responses())
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    assert result.primary_changed is False
    assert read_json(config_path) == payload

    output = "\n".join(io.messages)
    assert "欢迎使用 nanoBot 配置向导" in output
    assert "第 0 轮：环境检查" in output
    assert "保持当前值" in output
    assert "手动逐项检查" in output
    assert "Telegram: 已存在，未修改" in output
    assert "没有检测到配置变更，不会写入任何内容。" in output
    assert "telegram-old-token" not in output


def test_existing_telegram_update_token_only(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["unknownBlock"] = {"keep": True}
    write_json(config_path, payload)

    responses = ["", "", "", "2", "telegram-new-token", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    updated = read_json(config_path)
    assert result.wrote_primary is True
    assert updated["channels"]["telegram"]["token"] == "telegram-new-token"
    assert updated["channels"]["telegram"]["enabled"] is True
    assert updated["unknownBlock"] == {"keep": True}
    assert updated["gateway"] == payload["gateway"]
    assert result.primary_backup_path is not None
    assert read_json(result.primary_backup_path)["channels"]["telegram"]["token"] == "telegram-old-token"


def test_existing_telegram_update_token_omits_root_memory_on_write(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["memory"] = {"provider": "sqlite"}
    write_json(config_path, payload)

    responses = ["", "", "", "2", "telegram-new-token", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    updated = read_json(config_path)
    output = "\n".join(io.messages)

    assert result.wrote_primary is True
    assert updated["channels"]["telegram"]["token"] == "telegram-new-token"
    assert "memory" not in updated
    assert "旧格式处理说明：" in output
    assert "新写出的配置中将不再包含 root memory" in output


def test_root_memory_notice_is_shown_in_chinese_during_basics_review(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["memory"] = {"provider": "sqlite"}
    write_json(config_path, payload)

    responses = ["", "2", "", "", "", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    output = "\n".join(io.messages)
    assert "检测到旧格式的 root memory 配置。" in output
    assert "新的写出结果中将不再包含 root memory。" in output


def test_existing_telegram_modify_access_without_reprompting_token(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["unknownBlock"] = {"keep": "access"}
    write_json(config_path, payload)

    responses = [
        "",
        "",
        "",
        "7",
        "2",
        "alice,bob",
        "2",
        "2",
        "y",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    updated = read_json(config_path)
    assert result.wrote_primary is True
    assert updated["channels"]["telegram"]["token"] == payload["channels"]["telegram"]["token"]
    assert updated["channels"]["telegram"]["allowFrom"] == ["alice", "bob"]
    assert updated["channels"]["telegram"]["groupPolicy"] == "open"
    assert updated["unknownBlock"] == {"keep": "access"}

    output = "\n".join(io.messages)
    assert "请输入新的 Telegram token" not in output
    assert "Telegram: 已修改" in output


def test_existing_telegram_add_discord_without_touching_telegram(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["unknownBlock"] = {"keep": "yes"}
    write_json(config_path, payload)

    responses = [
        "",
        "",
        "",
        "4",
        "2",
        "discord-token",
        "",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    updated = read_json(config_path)
    assert result.wrote_primary is True
    assert updated["channels"]["telegram"] == payload["channels"]["telegram"]
    assert updated["channels"]["discord"]["enabled"] is True
    assert updated["channels"]["discord"]["token"] == "discord-token"
    assert updated["channels"]["discord"]["allowFrom"] == []
    assert updated["channels"]["discord"]["groupPolicy"] == "mention"
    assert updated["unknownBlock"] == {"keep": "yes"}

    output = "\n".join(io.messages)
    assert "Telegram: 已存在，未修改" in output
    assert "Discord: 本次新增" in output
    assert "Feishu: 未配置" in output


def test_cli_config_flag_only_targets_requested_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default_config = tmp_path / ".nanobot" / "config.json"
    custom_config = tmp_path / ".nanobot-telegram" / "config.json"
    default_payload = telegram_config(token="default-token")
    custom_payload = telegram_config(token="custom-token")
    write_json(default_config, default_payload)
    write_json(custom_config, custom_payload)

    responses = iter(keep_existing_responses())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(responses, ""))
    monkeypatch.setattr("nanobot.cli.wizard.getpass", lambda _prompt="": next(responses, ""))

    exit_code = wizard_main(["--config", str(custom_config)])

    assert exit_code == 0
    assert read_json(custom_config) == custom_payload
    assert read_json(default_config) == default_payload


def test_split_mixed_instance_into_standalone_telegram(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["channels"]["discord"] = {
        "enabled": True,
        "token": "discord-token",
        "allowFrom": ["ops-room"],
        "groupPolicy": "mention",
    }
    payload["memory"] = {"provider": "sqlite"}
    write_json(config_path, payload)

    split_path = suggest_split_config_path(config_path)
    responses = [
        "",
        "",
        "",
        "5",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    assert result.wrote_split is True
    assert result.split_config_path == split_path
    assert read_json(config_path) == payload

    split_payload = read_json(split_path)
    assert set(split_payload["channels"]) == {"sendProgress", "sendToolHints", "telegram"}
    assert split_payload["channels"]["telegram"] == payload["channels"]["telegram"]
    assert split_payload["agents"]["defaults"]["workspace"] == str(split_path.parent / "workspace")
    assert split_payload["gateway"]["port"] == payload["gateway"]["port"] + 1
    assert split_payload["tools"]["web"]["search"] == payload["tools"]["web"]["search"]
    assert "memory" not in split_payload

    output = "\n".join(io.messages)
    assert f"下一步命令：nanobot gateway --config {split_path}" in output
    assert "新写出的配置中将不再包含 root memory" in output


def test_legacy_fallback_fields_are_canonicalized_for_upstream(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "legacy.json"
    payload = {
        "model": {"default": "anthropic/claude-opus-4-5"},
        "paths": {"workspace": "/tmp/legacy-workspace"},
        "progressStreaming": False,
        "restrictToWorkspace": True,
        "channels": {
            "telegram": {
                "enabled": True,
                "token": "legacy-token",
            }
        },
        "gateway": {"port": 19001},
        "tools": {"web": {"search": {"apiKey": "", "maxResults": 5}}},
    }
    write_json(config_path, payload)

    io = ScriptedIO([""] * 7)
    result = run_wizard(config_path, io=io)

    updated = read_json(config_path)
    assert result.wrote_primary is True
    assert updated["agents"]["defaults"]["model"] == "anthropic/claude-opus-4-5"
    assert updated["agents"]["defaults"]["workspace"] == "/tmp/legacy-workspace"
    assert updated["channels"]["sendProgress"] is False
    assert updated["tools"]["restrictToWorkspace"] is True
    assert "model" not in updated
    assert "paths" not in updated
    assert "progressStreaming" not in updated
    assert "restrictToWorkspace" not in updated
    assert "检测到旧格式字段，当前配置会改写为规范路径：" in "\n".join(io.messages)


def test_invalid_json_reports_line_and_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('{"channels":\n', encoding="utf-8")

    printed: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr("builtins.print", lambda *args, **_kwargs: printed.append(" ".join(map(str, args))))

    exit_code = wizard_main(["--config", str(config_path)])

    assert exit_code == 2
    output = "\n".join(printed)
    assert str(config_path) in output
    assert "第 2 行，第 1 列" in output


def test_keyboard_interrupt_before_confirmation_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    printed: list[str] = []

    def raise_interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", raise_interrupt)
    monkeypatch.setattr("builtins.print", lambda *args, **_kwargs: printed.append(" ".join(map(str, args))))

    exit_code = wizard_main(["--config", str(config_path)])

    assert exit_code == 130
    assert read_json(config_path) == payload
    assert "已中断。没有写入任何修改。" in "\n".join(printed)


def test_allow_from_wildcard_requires_second_confirmation(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = [
        "",
        "",
        "5",
        "2",
        "*",
        "n",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    assert read_json(config_path) == payload
    output = "\n".join(io.messages)
    assert "警告：你即将启用较高风险的配置：" in output
    assert '- allowFrom = ["*"]' in output


def test_group_policy_open_requires_second_confirmation(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = [
        "",
        "",
        "5",
        "",
        "2",
        "2",
        "n",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    assert read_json(config_path) == payload
    output = "\n".join(io.messages)
    assert "默认渠道 groupPolicy = open" in output
    assert "警告：你即将启用较高风险的配置：" in output


def test_restrict_to_workspace_disable_requires_warning_and_confirmation(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = [
        "",
        "",
        "5",
        "",
        "",
        "2",
        "n",
        "n",
        "",
        "",
        "",
    ]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    assert read_json(config_path) == payload
    output = "\n".join(io.messages)
    assert "警告：tools.restrictToWorkspace = false 会放宽到当前工作目录之外的文件访问。" in output
    assert "tools.restrictToWorkspace = false" in output


def test_security_preset_menu_shows_preview_and_apply_confirmation_in_chinese(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    write_json(config_path, telegram_config())

    io = ScriptedIO(["", "", "3", "", "", "", ""])
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    output = "\n".join(io.messages)
    assert "第 2 轮：安全配置" in output
    assert "推荐：[3] 平衡模式" in output
    assert "本预设将处理以下字段：" in output
    assert "是否应用这个预设？" in output


def test_recommendation_list_can_exit_without_applying_changes(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = ["", "", "", "2", "telegram-new-token", "", "3", "3"]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.canceled is True
    assert read_json(config_path) == payload
    output = "\n".join(io.messages)
    assert "推荐列表：" in output
    assert "接下来你想做什么？" in output
    assert f"目标配置文件：\n{config_path}" not in output


def test_recommendation_commands_show_follow_up_menu_before_summary(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = ["", "", "", "2", "telegram-new-token", "", "2", "1", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is True
    assert read_json(config_path)["channels"]["telegram"]["token"] == "telegram-new-token"
    output = "\n".join(io.messages)
    assert "推荐安装命令如下：" in output
    assert "接下来你想做什么？" in output
    assert "nanobot skills install workspace-guard" in output
    assert "配置摘要" in output


def test_english_mode_full_flow_updates_existing_telegram(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = ["2", "", "", "2", "telegram-new-token", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is True
    assert read_json(config_path)["channels"]["telegram"]["token"] == "telegram-new-token"
    output = "\n".join(io.messages)
    assert "Welcome to the nanoBot setup wizard" in output
    assert "This wizard is useful for both first-time setup and careful maintenance of an existing instance." in output
    assert "Round 0 - environment check" in output
    assert "Choose how to handle the basic settings for this instance." in output
    assert "Configuration summary" in output
    assert "Yes: back up the current config first, then write the updated result." in output


def test_english_security_preset_menu_and_preview_explain_benefits_and_tradeoffs(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    write_json(config_path, telegram_config())

    io = ScriptedIO(["2", "", "3", "", "", "", ""])
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    output = "\n".join(io.messages)
    assert "Choose a security preset." in output
    assert "Why presets exist:" in output
    assert "Benefit: protects workspace access while keeping normal day-to-day use comfortable." in output
    assert "Trade-off: still expects you to be intentional about broad access rules." in output
    assert "Why people choose this preset:" in output
    assert "This preview exists so you can understand the bundle before applying it." in output


def test_english_existing_telegram_maintenance_mode_is_clear(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    io = ScriptedIO(["2", "", "", "", "", ""])
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is False
    output = "\n".join(io.messages)
    assert "Existing Telegram maintenance mode" in output
    assert "This instance already has a working Telegram bot." in output
    assert "The default action is to keep it unchanged" in output
    assert "configured (" in output
    assert "Enter the new Telegram token" not in output


def test_english_recommendation_viewing_can_exit_without_reaching_summary(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    write_json(config_path, payload)

    responses = ["2", "", "", "2", "telegram-new-token", "", "3", "3"]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.canceled is True
    assert read_json(config_path) == payload
    output = "\n".join(io.messages)
    assert "Recommendation list:" in output
    assert "What would you like to do next?" in output
    assert "Only option [1] continues to the configuration summary." in output
    assert f"Target config file:\n{config_path}" not in output


def test_english_summary_explains_legacy_normalization_and_root_memory(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["memory"] = {"provider": "sqlite"}
    write_json(config_path, payload)

    responses = ["2", "", "", "2", "telegram-new-token", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_primary is True
    output = "\n".join(io.messages)
    assert "Compatibility notes:" in output
    assert "Old format detected:" in output
    assert "Newly written config will omit root memory." in output
    assert "compatible with the current upstream schema" in output


def test_english_split_flow_explains_why_dedicated_instance_is_useful(tmp_path: Path) -> None:
    config_path = tmp_path / ".nanobot" / "config.json"
    payload = telegram_config()
    payload["channels"]["discord"] = {
        "enabled": True,
        "token": "discord-token",
        "allowFrom": ["ops-room"],
        "groupPolicy": "mention",
    }
    write_json(config_path, payload)

    split_path = suggest_split_config_path(config_path)
    responses = ["2", "", "", "5", "", "", "", "", "", "", "", "", ""]
    io = ScriptedIO(responses)
    result = run_wizard(config_path, io=io)

    assert result.wrote_split is True
    assert result.split_config_path == split_path
    output = "\n".join(io.messages)
    assert "A dedicated Telegram instance is useful when you want cleaner isolation" in output
    assert "Why this can help: Telegram gets its own workspace, runtime layout, and port boundary." in output
    assert "Trade-off: you will manage one more config file and one more instance process." in output
    assert f"Next command to start the dedicated instance: nanobot gateway --config {split_path}" in output
