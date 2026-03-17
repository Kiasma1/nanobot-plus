from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from nanobot.cli.wizard import run_wizard, suggest_split_config_path
from nanobot.config.loader import load_config, set_config_path
from nanobot.config.wizard_mapper import get_path


class ScriptedIO:
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

    def choose(self, title: str, options, default: str):
        option_lines = [title]
        for key, label in options:
            marker = " (default)" if key == default else ""
            option_lines.append(f"[{key}] {label}{marker}")
        self.write("\n".join(option_lines))
        raw = self.prompt("Select: ").strip()
        return raw or default

    def confirm(self, message: str, *, default: bool):
        suffix = "[Y/n]" if default else "[y/N]"
        raw = self.prompt(f"{message} {suffix} ").strip().lower()
        if not raw:
            return default
        return raw in {"y", "yes"}

    def ask_text(self, message: str, *, default: str | None = None, allow_empty: bool = False):
        prompt = message
        if default not in (None, ""):
            prompt += f" [{default}]"
        prompt += ": "
        raw = self.prompt(prompt)
        if raw == "" and default is not None:
            return default
        if raw == "" and allow_empty:
            return ""
        return raw


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def telegram_config(token: str = "telegram-old-token") -> dict[str, Any]:
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
            },
        },
        "gateway": {
            "host": "0.0.0.0",
            "port": 18790,
            "heartbeat": {"enabled": True, "intervalS": 1800},
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


def validate_with_main_repo(config_path: Path) -> dict[str, Any]:
    set_config_path(config_path)
    config = load_config(config_path)
    data = config.model_dump(by_alias=True)
    channels = sorted(
        name
        for name, section in data.get("channels", {}).items()
        if isinstance(section, dict) and section.get("enabled")
    )
    return {
        "workspace": data.get("agents", {}).get("defaults", {}).get("workspace"),
        "model": data.get("agents", {}).get("defaults", {}).get("model"),
        "gatewayPort": data.get("gateway", {}).get("port"),
        "sendProgress": data.get("channels", {}).get("sendProgress"),
        "sendToolHints": data.get("channels", {}).get("sendToolHints"),
        "restrictToWorkspace": data.get("tools", {}).get("restrictToWorkspace"),
        "braveApiKeyConfigured": bool(data.get("tools", {}).get("web", {}).get("search", {}).get("apiKey")),
        "enabledChannels": channels,
    }


def expect_summary(summary: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for key, value in expected.items():
        if summary.get(key) != value:
            mismatches.append(f"{key}: expected {value!r}, got {summary.get(key)!r}")
    return mismatches


def scenario_default_instance(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot" / "config.json"
    io = ScriptedIO(["", "", "", "", "", "", "y"])
    run_wizard(config_path, io=io)
    summary = validate_with_main_repo(config_path)
    return {
        "scenario": "default_instance_new_write",
        "config": str(config_path),
        "summary": summary,
        "summaryMismatches": expect_summary(
            summary,
            {
                "workspace": str(config_path.parent / "workspace"),
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18790,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": [],
            },
        ),
    }


def scenario_custom_config_token_rotate(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot-telegram" / "config.json"
    write_json(config_path, telegram_config())
    io = ScriptedIO(["", "", "", "2", "telegram-new-token", "", "", ""])
    run_wizard(config_path, io=io)
    data = read_json(config_path)
    summary = validate_with_main_repo(config_path)
    return {
        "scenario": "custom_config_existing_telegram_token_rotate",
        "config": str(config_path),
        "tokenSuffix": data["channels"]["telegram"]["token"][-4:],
        "summary": summary,
        "summaryMismatches": expect_summary(
            summary,
            {
                "workspace": "/tmp/nanobot/workspace",
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18790,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["telegram"],
            },
        ),
    }


def scenario_existing_telegram_access(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot" / "existing-telegram.json"
    write_json(config_path, telegram_config())
    io = ScriptedIO(
        [
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
    )
    run_wizard(config_path, io=io)
    data = read_json(config_path)
    summary = validate_with_main_repo(config_path)
    return {
        "scenario": "existing_telegram_access_maintenance",
        "config": str(config_path),
        "allowFrom": data["channels"]["telegram"]["allowFrom"],
        "groupPolicy": data["channels"]["telegram"]["groupPolicy"],
        "summary": summary,
        "summaryMismatches": expect_summary(
            summary,
            {
                "workspace": "/tmp/nanobot/workspace",
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18790,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["telegram"],
            },
        ),
    }


def scenario_mixed_instance_split(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot" / "mixed.json"
    payload = telegram_config()
    payload["channels"]["discord"] = {
        "enabled": True,
        "token": "discord-token",
        "allowFrom": ["ops-room"],
        "gatewayUrl": "wss://gateway.discord.gg/?v=10&encoding=json",
        "intents": 37377,
        "groupPolicy": "mention",
    }
    write_json(config_path, payload)
    split_path = suggest_split_config_path(config_path)
    io = ScriptedIO(
        [
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
        ]
    )
    run_wizard(config_path, io=io)
    original_summary = validate_with_main_repo(config_path)
    split_summary = validate_with_main_repo(split_path)
    return {
        "scenario": "mixed_channels_split_to_standalone_telegram",
        "config": str(config_path),
        "splitConfig": str(split_path),
        "summaryOriginal": original_summary,
        "summarySplit": split_summary,
        "summaryMismatchesOriginal": expect_summary(
            original_summary,
            {
                "workspace": "/tmp/nanobot/workspace",
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18790,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["discord", "telegram"],
            },
        ),
        "summaryMismatchesSplit": expect_summary(
            split_summary,
            {
                "workspace": str(split_path.parent / "workspace"),
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18791,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["telegram"],
            },
        ),
    }


def scenario_legacy_fallback(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot" / "legacy.json"
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
    run_wizard(config_path, io=io)
    data = read_json(config_path)
    summary = validate_with_main_repo(config_path)
    return {
        "scenario": "legacy_fallback_canonicalized",
        "config": str(config_path),
        "canonicalModel": get_path(data, ("agents", "defaults", "model")),
        "canonicalWorkspace": get_path(data, ("agents", "defaults", "workspace")),
        "sendProgress": get_path(data, ("channels", "sendProgress")),
        "restrictToWorkspace": get_path(data, ("tools", "restrictToWorkspace")),
        "summary": summary,
        "summaryMismatches": expect_summary(
            summary,
            {
                "workspace": "/tmp/legacy-workspace",
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 19001,
                "sendProgress": False,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["telegram"],
            },
        ),
    }


def scenario_memory_root_omitted_on_write(tmp_root: Path) -> dict[str, Any]:
    config_path = tmp_root / ".nanobot" / "memory-write.json"
    payload = telegram_config()
    payload["memory"] = {"provider": "sqlite"}
    write_json(config_path, payload)
    io = ScriptedIO(["", "", "", "2", "telegram-new-token", "", "", ""])
    run_wizard(config_path, io=io)
    data = read_json(config_path)
    summary = validate_with_main_repo(config_path)
    return {
        "scenario": "memory_root_omitted_on_primary_write",
        "config": str(config_path),
        "memoryPresentAfterWrite": "memory" in data,
        "summary": summary,
        "summaryMismatches": expect_summary(
            summary,
            {
                "workspace": "/tmp/nanobot/workspace",
                "model": "anthropic/claude-opus-4-5",
                "gatewayPort": 18790,
                "sendProgress": True,
                "sendToolHints": False,
                "restrictToWorkspace": True,
                "braveApiKeyConfigured": False,
                "enabledChannels": ["telegram"],
            },
        ),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="nanobot-smoke-") as temp_dir:
        root = Path(temp_dir)
        results = [
            scenario_default_instance(root),
            scenario_custom_config_token_rotate(root),
            scenario_existing_telegram_access(root),
            scenario_mixed_instance_split(root),
            scenario_legacy_fallback(root),
            scenario_memory_root_omitted_on_write(root),
        ]

    blockers = [
        item["scenario"]
        for item in results
        if item.get("summaryMismatches")
        or item.get("summaryMismatchesOriginal")
        or item.get("summaryMismatchesSplit")
        or item.get("memoryPresentAfterWrite") is True
    ]

    print(json.dumps({"scenarios": results, "blockingScenarios": blockers}, ensure_ascii=False, indent=2))
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
