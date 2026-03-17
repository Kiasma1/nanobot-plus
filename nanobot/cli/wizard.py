from __future__ import annotations

import argparse
from dataclasses import dataclass
from getpass import getpass
import json
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Sequence

from nanobot.config.wizard_backup import backup_config
from nanobot.config.wizard_diff import diff_configs, render_summary
from nanobot.config.wizard_mapper import (
    canonicalize_bindings,
    compact_json_value,
    deep_copy_config,
    delete_path,
    describe_binding_rewrites,
    get_path,
    mask_secret,
    read_binding,
    set_binding,
    set_path,
    strip_root_memory,
)
from nanobot.config.wizard_schema import (
    CHANNEL_DEFAULT_CONFIGS,
    CHANNEL_ACCESS_FIELDS,
    CHANNEL_ORDER,
    DEFAULT_AGENT_MODEL,
    DEFAULT_CHANNEL_ALLOW_FROM,
    DEFAULT_CHANNEL_GROUP_POLICY,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_SEARCH_MAX_RESULTS,
    GATEWAY_PORT_FIELD,
    LEGACY_FALLBACK_BINDINGS,
    MODEL_FIELD,
    RECOMMENDED_SKILLS,
    SEARCH_API_KEY_FIELD,
    SEARCH_MAX_RESULTS_FIELD,
    SECURITY_PRESETS,
    SEND_PROGRESS_FIELD,
    SEND_TOOL_HINTS_FIELD,
    TOOLS_RESTRICT_FIELD,
    WORKSPACE_FIELD,
    group_policy_options,
    keep_modify_skip_options,
    security_preset_options,
    skills_follow_up_options,
    skills_step_options,
    telegram_maintenance_options,
    telegram_new_options,
)
from nanobot.config.wizard_writer import atomic_write_json


class ConfigLoadError(RuntimeError):
    """Raised when the current config cannot be loaded."""


@dataclass(frozen=True)
class InstanceInfo:
    config_path: Path
    workspace: str | None
    gateway_port: int | None
    enabled_channels: list[str]
    web_search_provider: str | None
    telegram_connected: bool


@dataclass
class SplitPlan:
    config_path: Path
    draft_config: dict[str, Any]
    next_command: str
    pure_telegram: bool
    copy_search: bool


@dataclass
class WizardResult:
    wrote_primary: bool = False
    primary_backup_path: Path | None = None
    wrote_split: bool = False
    split_backup_path: Path | None = None
    split_config_path: Path | None = None
    canceled: bool = False
    primary_changed: bool = False


class WizardIO:
    language = "zh"

    def write(self, message: str = "") -> None:
        raise NotImplementedError

    def prompt(self, message: str) -> str:
        raise NotImplementedError

    def prompt_secret(self, message: str) -> str:
        return self.prompt(message)

    def choose(self, title: str, options: Sequence[tuple[str, str]], default: str) -> str:
        language = getattr(self, "language", "en")
        option_lines = [title]
        for key, label in options:
            marker = "（默认）" if language == "zh" and key == default else ""
            if language != "zh" and key == default:
                marker = " (default)"
            option_lines.append(f"[{key}] {label}{marker}")
        self.write("\n".join(option_lines))

        valid = {key for key, _ in options}
        while True:
            prompt_text = "输入选项：" if language == "zh" else "Select an option: "
            raw = self.prompt(prompt_text).strip()
            if not raw:
                return default
            if raw in valid:
                return raw
            self.write(f"无效选项：{raw}" if language == "zh" else f"Invalid choice: {raw}")

    def confirm(self, message: str, *, default: bool) -> bool:
        language = getattr(self, "language", "en")
        suffix = "[Y/n]" if default else "[y/N]"
        while True:
            raw = self.prompt(f"{message} {suffix} ").strip().lower()
            if not raw:
                return default
            if raw in {"y", "yes"}:
                return True
            if raw in {"n", "no"}:
                return False
            self.write(f"无效确认：{raw}" if language == "zh" else f"Invalid confirmation: {raw}")

    def ask_text(self, message: str, *, default: str | None = None, allow_empty: bool = False) -> str:
        language = getattr(self, "language", "en")
        prompt = f"{message}"
        if default not in (None, ""):
            prompt += f" [{default}]"
        prompt += "： " if language == "zh" else ": "

        while True:
            raw = self.prompt(prompt)
            if raw == "" and default is not None:
                return default
            if raw == "" and allow_empty:
                return ""
            if raw != "":
                return raw
            self.write("该值不能为空。" if language == "zh" else "This value cannot be empty.")


class ConsoleIO(WizardIO):
    def __init__(
        self,
        *,
        input_func: Callable[[str], str] | None = None,
        output_func: Callable[[str], None] | None = None,
    ) -> None:
        self._input = input_func or input
        self._output = output_func or print
        self.language = "zh"

    def write(self, message: str = "") -> None:
        self._output(message)

    def prompt(self, message: str) -> str:
        return self._input(message)

    def prompt_secret(self, message: str) -> str:
        return getpass(message)


class Wizard:
    def __init__(self, config_path: Path, io: WizardIO) -> None:
        self.config_path = config_path
        self.io = io
        self.language = "zh"
        self.config_exists = False
        self.original_config: dict[str, Any] = {}
        self.draft_config: dict[str, Any] = {}
        self.info: InstanceInfo | None = None
        self.split_plan: SplitPlan | None = None
        self.telegram_connected_at_start = False
        self.exit_without_applying = False
        self.channel_security_defaults = {
            "allowFrom": list(DEFAULT_CHANNEL_ALLOW_FROM),
            "groupPolicy": DEFAULT_CHANNEL_GROUP_POLICY,
        }

    def run(self) -> WizardResult:
        self._choose_language()
        self._load_config()
        self._run_round_0()
        self._run_round_1()
        self._run_round_2()
        self._run_round_3()
        self._run_round_4()
        if self.exit_without_applying:
            self.io.write(self._t("已取消。\n没有写入任何修改。", "Canceled.\nNo changes written."))
            return WizardResult(canceled=True)
        return self._review_and_write()

    @property
    def is_zh(self) -> bool:
        return self.language == "zh"

    def _t(self, zh: str, en: str) -> str:
        return zh if self.is_zh else en

    def _choose_language(self) -> None:
        intro = [
            "欢迎使用 nanoBot 配置向导",
            "",
            "这个向导会先读取当前配置，再分步骤收集修改项。",
            "在你最终确认之前，不会写入任何配置文件。",
            "",
            "Welcome to the nanoBot setup wizard",
            "",
            "This wizard is useful for both first-time setup and careful maintenance of an existing instance.",
            "It reads your current configuration first, then collects changes step by step.",
            "No configuration will be written until you confirm at the end.",
            "",
            "请选择语言 / Choose language:",
            "",
            "[1] 中文（默认）",
            "[2] English",
        ]
        self.io.write("\n".join(intro))
        while True:
            raw = self.io.prompt("输入选项 / Select an option: ").strip()
            if raw in {"", "1"}:
                self.language = "zh"
                self.io.language = "zh"
                return
            if raw == "2":
                self.language = "en"
                self.io.language = "en"
                return
            self.io.write("无效选项，请输入 1 或 2。" if self.is_zh else "Invalid choice. Enter 1 or 2.")

    def _load_config(self) -> None:
        self.config_exists = self.config_path.exists()

        if self.config_exists:
            try:
                self.original_config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except JSONDecodeError as exc:
                raise ConfigLoadError(
                    self._t(
                        f"解析 JSON 失败：{self.config_path}，第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}",
                        f"Failed to parse JSON at {self.config_path} line {exc.lineno}, column {exc.colno}: {exc.msg}",
                    )
                ) from exc
        else:
            self.original_config = {}

        self.draft_config = deep_copy_config(self.original_config) if self.original_config else self._minimal_config()
        self.info = detect_instance(self.config_path, self.original_config, self.language)
        self.telegram_connected_at_start = self.info.telegram_connected
        self.channel_security_defaults = detect_channel_security_defaults(self.original_config)

    def _minimal_config(self) -> dict[str, Any]:
        return {
            "agents": {
                "defaults": {
                    "workspace": default_instance_workspace(self.config_path),
                    "model": DEFAULT_AGENT_MODEL,
                }
            },
            "channels": {
                "sendProgress": True,
                "sendToolHints": False,
            },
            "gateway": {
                "host": "0.0.0.0",
                "port": DEFAULT_GATEWAY_PORT,
                "heartbeat": {
                    "enabled": True,
                    "intervalS": 30 * 60,
                },
            },
            "tools": {
                "web": {
                    "search": {
                        "apiKey": "",
                        "maxResults": DEFAULT_SEARCH_MAX_RESULTS,
                    }
                },
                "exec": {"timeout": 60, "pathAppend": ""},
                "restrictToWorkspace": False,
                "mcpServers": {},
            },
        }

    def _run_round_0(self) -> None:
        assert self.info is not None

        enabled = ", ".join(self.info.enabled_channels) if self.info.enabled_channels else self._t("未配置", "(none)")
        web_search = self.info.web_search_provider or self._t("未设置", "(unset)")

        self.io.write(self._t("第 0 轮：环境检查", "Round 0 - environment check"))
        self.io.write("")
        self.io.write(self._t("当前配置文件：", "Current config file:"))
        self.io.write(str(self.config_path))
        self.io.write("")
        self.io.write(self._t("检测结果：", "Detection result:"))
        self.io.write(
            f"- {self._t('配置文件', 'Config file')}: "
            f"{self._t('已存在' if self.config_exists else '不存在', 'exists' if self.config_exists else 'missing')}"
        )
        self.io.write(f"- {self._t('工作目录', 'Workspace')}: {self.info.workspace or self._t('未设置', '(unset)')}")
        self.io.write(
            f"- {self._t('网关端口', 'Gateway port')}: "
            f"{self.info.gateway_port if self.info.gateway_port is not None else self._t('未设置', '(unset)')}"
        )
        self.io.write(f"- {self._t('已启用渠道', 'Enabled channels')}: {enabled}")
        self.io.write(f"- {self._t('搜索提供方', 'Search provider')}: {web_search}")
        self.io.write("")
        self.io.write(self._t("说明：", "Notes:"))
        self.io.write(
            f"- {self._t('如果配置文件不存在，将从最小配置草稿开始。', 'If the config file does not exist, the wizard starts from a minimal draft so you can get a valid instance without hand-writing JSON.')}"
        )
        self.io.write(
            f"- {self._t('这一步是只读检查，尤其适合在使用 --config 时确认你操作的是正确实例。', 'This read-only check helps you confirm that you are editing the right instance, especially when you pass --config.')}"
        )
        self.io.write(
            f"- {self._t('在最终确认之前，不会写入配置文件。', 'No configuration will be written before the final confirmation.')}"
        )
        self.io.write("")

    def _run_round_1(self) -> None:
        self.io.write(self._t("第 1 轮：基础配置", "Round 1 - basics"))
        self.io.write(
            self._t(
                "请选择要处理的基础配置项。\n你可以保持不变，也可以逐项修改。",
                "Choose how to handle the basic settings for this instance.\n\n"
                "- Keep current values: useful when the instance already works and you want to move on quickly.\n"
                "- Review fields manually: useful when you want to inspect each field, understand why it exists, and decide case by case.",
            )
        )
        self.io.write("")
        self.io.write(self._t("当前值预览：", "Current values:"))
        self.io.write(f"- {self._t('默认模型', 'Default model')}: {read_binding(self.draft_config, MODEL_FIELD) or self._t('未设置', '(unset)')}")
        self.io.write(f"- {self._t('工作目录', 'Workspace')}: {read_binding(self.draft_config, WORKSPACE_FIELD) or self._t('未设置', '(unset)')}")
        self.io.write(f"- {self._t('搜索提供方', 'Web search')}: {detect_web_search_status(self.draft_config, self.language)}")
        self.io.write(
            f"- {self._t('工作区限制', 'Restrict to workspace')}: "
            f"{bool(read_binding(self.draft_config, TOOLS_RESTRICT_FIELD))}"
        )
        self.io.write(f"- {self._t('过程提示', 'Send progress')}: {bool(read_binding(self.draft_config, SEND_PROGRESS_FIELD))}")
        self.io.write(f"- {self._t('工具提示', 'Send tool hints')}: {bool(read_binding(self.draft_config, SEND_TOOL_HINTS_FIELD))}")
        choice = self.io.choose(
            self._t(
                "请选择后续方式：\n\n说明：\n- “保持当前值” = 本轮基础配置全部保持现状\n- “手动逐项检查” = 继续查看每个字段并决定是否修改",
                "Choose how to continue:\n\n- Keep current values: skip the basic settings round.\n- Review fields manually: inspect each field before deciding.",
            ),
            [
                ("1", self._t("保持当前值", "Keep current values")),
                ("2", self._t("手动逐项检查", "Review fields manually")),
            ],
            default="1",
        )
        if choice == "1":
            self.io.write("")
            return

        self._configure_default_model()
        self._configure_workspace()
        self._configure_web_search()
        self._configure_channel_feedback()
        self._show_memory_note()
        self.io.write("")

    def _run_round_2(self) -> None:
        self.io.write(self._t("第 2 轮：安全配置", "Round 2 - security"))
        manual_review = self._configure_security_preset()
        if manual_review:
            self._configure_channel_access_defaults()
            self._configure_restrict_to_workspace()
        self.io.write("")

    def _run_round_3(self) -> None:
        self.io.write(self._t("第 3 轮：渠道配置", "Round 3 - channels"))
        review_other_channels = self._configure_telegram()
        if review_other_channels:
            self._configure_discord()
            self._configure_feishu()
        self.io.write("")

    def _run_round_4(self) -> None:
        self.io.write(self._t("轻量推荐步骤", "Lightweight recommendation step"))
        self.io.write(
            self._t(
                "这一步仅用于查看建议。\n查看推荐内容本身不会应用任何修改。",
                "This step is informational only.\n"
                "It exists so you can see optional follow-up improvements after the core config is settled.\n"
                "Viewing recommendations will not apply any config changes.",
            )
        )
        self.io.write("")

        while True:
            choice = self.io.choose(
                self._t("请选择：", "Choose an option:"),
                skills_step_options(self.language),
                default="1",
            )

            if choice == "1":
                self.io.write(self._t("已跳过推荐步骤。", "Skipping recommended skills."))
                self.io.write("")
                return

            if choice == "2":
                self.io.write(self._t("推荐安装命令如下：", "Suggested install commands:"))
                for skill in RECOMMENDED_SKILLS:
                    self.io.write(f"nanobot skills install {skill['slug']}")
            else:
                self.io.write(self._t("推荐列表：", "Recommendation list:"))
                for skill in RECOMMENDED_SKILLS:
                    self.io.write(
                        f"- {localized_skill_title(skill, self.language)}："
                        f"{localized_skill_description(skill, self.language)}"
                    )

            self.io.write("")
            next_step = self.io.choose(
                self._t(
                    "接下来你想做什么？",
                    "What would you like to do next?\n\nOnly option [1] continues to the configuration summary.",
                ),
                skills_follow_up_options(self.language),
                default="1",
            )
            if next_step == "1":
                self.io.write("")
                return
            if next_step == "2":
                self.io.write("")
                continue

            self.exit_without_applying = True
            self.io.write("")
            return

    def _review_and_write(self) -> WizardResult:
        primary_rewrites = describe_binding_rewrites(self.draft_config, LEGACY_FALLBACK_BINDINGS)
        canonicalize_bindings(self.draft_config, LEGACY_FALLBACK_BINDINGS)

        primary_baseline_changed = bool(diff_configs(self.original_config, self.draft_config)) or not self.config_exists
        primary_memory_removed = False
        if primary_baseline_changed:
            primary_memory_removed = strip_root_memory(self.draft_config)

        split_rewrites: list[str] = []
        split_memory_removed = False
        if self.split_plan is not None:
            split_rewrites = describe_binding_rewrites(self.split_plan.draft_config, LEGACY_FALLBACK_BINDINGS)
            canonicalize_bindings(self.split_plan.draft_config, LEGACY_FALLBACK_BINDINGS)
            split_memory_removed = strip_root_memory(self.split_plan.draft_config)

        summary_notes = build_legacy_handling_notes(
            primary_rewrites=primary_rewrites,
            split_rewrites=split_rewrites,
            primary_memory_removed=primary_memory_removed,
            split_memory_removed=split_memory_removed,
            primary_will_write=primary_baseline_changed,
            has_split=self.split_plan is not None,
            current_config_has_memory="memory" in self.original_config,
            language=self.language,
        )

        split_summary = self._render_split_summary()
        summary = render_summary(
            self.original_config,
            self.draft_config,
            self.config_path,
            language=self.language,
            notes=summary_notes,
            split_summary=split_summary,
        )
        self.io.write(summary)
        self.io.write("")

        primary_changed = bool(diff_configs(self.original_config, self.draft_config)) or not self.config_exists
        split_changed = self.split_plan is not None
        result = WizardResult(primary_changed=primary_changed)

        if not primary_changed and not split_changed:
            self.io.write(self._t("没有检测到配置变更，不会写入任何内容。", "No config changes detected. Nothing written."))
            return result

        if self.is_zh:
            self.io.write("注意：")
            self.io.write("- 选择“是”后，将先备份原配置，再写入新配置")
            self.io.write("- 选择“否”后，不会写入任何修改")
        else:
            self.io.write("Notes:")
            self.io.write("- Yes: back up the current config first, then write the updated result.")
            self.io.write("- No: exit without applying changes.")
            self.io.write("- This is the final checkpoint before any file on disk is changed.")

        if not self.io.confirm(self._t("是否应用以上修改？", "Apply these changes?"), default=True):
            self.io.write(self._t("已取消。\n没有写入任何修改。", "Canceled.\nNo changes written."))
            result.canceled = True
            return result

        if primary_changed:
            self.io.write(self._t("正在备份原配置...", "Backing up the current config..."))
            result.primary_backup_path = backup_config(self.config_path)
            if result.primary_backup_path is not None:
                self.io.write(
                    self._t(
                        f"备份完成：{result.primary_backup_path}",
                        f"Backup written to: {result.primary_backup_path}",
                    )
                )
            self.io.write(self._t("正在写入新配置...", "Writing the updated config..."))
            atomic_write_json(self.config_path, self.draft_config)
            result.wrote_primary = True
            self.io.write(self._t(f"写入完成：{self.config_path}", f"Config written to: {self.config_path}"))

        if self.split_plan is not None:
            self.io.write("")
            self.io.write(self._t("正在备份独立 Telegram 实例配置...", "Backing up the standalone Telegram config..."))
            result.split_backup_path = backup_config(self.split_plan.config_path)
            if result.split_backup_path is not None:
                self.io.write(
                    self._t(
                        f"备份完成：{result.split_backup_path}",
                        f"Backup written to: {result.split_backup_path}",
                    )
                )
            self.io.write(self._t("正在写入独立 Telegram 实例配置...", "Writing the standalone Telegram config..."))
            atomic_write_json(self.split_plan.config_path, self.split_plan.draft_config)
            result.wrote_split = True
            result.split_config_path = self.split_plan.config_path
            self.io.write(self._t(f"写入完成：{self.split_plan.config_path}", f"Config written to: {self.split_plan.config_path}"))
            self.io.write(self._t("下一步建议：", "Next steps:"))
            self.io.write(
                f"- {self._t('如有需要，使用以下命令启动：', 'Start the dedicated instance with:')}\n  {self.split_plan.next_command}"
            )

        if result.wrote_primary and self.split_plan is None:
            self.io.write(self._t("下一步建议：", "Next steps:"))
            self.io.write(
                f"- {self._t('重新检查配置摘要。', 'Review the summary again if needed.')}"
            )
            self.io.write(
                f"- {self._t('如有需要，使用以下命令启动：', 'Run with:')}\n  nanobot gateway --config {self.config_path}"
            )

        return result

    def _configure_default_model(self) -> None:
        current = read_binding(self.draft_config, MODEL_FIELD)
        action = self._field_action(
            label=self._t("默认模型", "Default model"),
            current=current,
            recommendation=self._t(
                "除非你准备切换实例用途，否则建议保持现有模型。",
                "The default model sets the baseline quality, cost, and behavior for this instance. "
                "Keep it when the current behavior is already right; change it only when this instance has a different job or budget target.",
            ),
        )
        if action != "2":
            return

        new_value = self.io.ask_text(
            self._t("请输入默认模型", "Enter the default model"),
            default=str(current) if current else None,
        )
        set_binding(self.draft_config, MODEL_FIELD, new_value, clear_fallbacks=True)

    def _configure_workspace(self) -> None:
        suggestion = default_instance_workspace(self.config_path)
        current = read_binding(self.draft_config, WORKSPACE_FIELD)
        action = self._field_action(
            label=self._t("工作目录", "Workspace path"),
            current=current,
            recommendation=self._t(
                f"建议使用实例级工作目录，例如 {suggestion}。",
                f"An instance-scoped workspace such as {suggestion} keeps files and runtime artifacts easier to reason about. "
                "Shared workspaces can be convenient, but they make it easier to mix contexts across instances.",
            ),
        )
        if action != "2":
            return

        new_value = self.io.ask_text(
            self._t("请输入工作目录路径", "Enter the workspace path"),
            default=current or suggestion,
        )
        set_binding(self.draft_config, WORKSPACE_FIELD, new_value, clear_fallbacks=True)

    def _configure_web_search(self) -> None:
        current_key = read_binding(self.draft_config, SEARCH_API_KEY_FIELD) or ""
        current_max_results = read_binding(self.draft_config, SEARCH_MAX_RESULTS_FIELD) or DEFAULT_SEARCH_MAX_RESULTS
        current_label = (
            self._t(f"已配置（{mask_secret(current_key)}）", f"configured ({mask_secret(current_key)})")
            if current_key
            else self._t("未设置", "(unset)")
        )
        action = self._field_action(
            label=self._t("Brave Search 配置", "Brave Search API key"),
            current=current_label,
            recommendation=self._t(
                "nanobot-plus 当前使用 Brave Search。可在这里配置 apiKey，也可以通过 BRAVE_API_KEY 提供。",
                "Brave Search is what this repo uses for web lookup. Configure it if this instance should answer current-event or reference-heavy questions. "
                "The trade-off is one more external dependency and API key to manage.",
            ),
        )
        if action != "2":
            return

        new_key = self.io.prompt_secret(
            self._t("请输入 Brave Search API key（留空表示清空）：", "Enter the Brave Search API key (leave blank to clear): ")
        )
        max_results_raw = self.io.ask_text(
            self._t("请输入 Brave Search maxResults", "Enter Brave Search maxResults"),
            default=str(current_max_results),
        )
        set_binding(self.draft_config, SEARCH_API_KEY_FIELD, new_key, clear_fallbacks=True)
        set_binding(self.draft_config, SEARCH_MAX_RESULTS_FIELD, int(max_results_raw), clear_fallbacks=True)

    def _configure_channel_feedback(self) -> None:
        send_progress = read_binding(self.draft_config, SEND_PROGRESS_FIELD)
        self.io.write(
            self._t(
                f"当前过程反馈使用 channels.sendProgress = {send_progress}。\n这与当前 nanoBot 的渠道配置结构一致。",
                f"Progress and tool hints control how much execution detail users can see. "
                f"Right now channels.sendProgress = {send_progress}. More detail helps debugging and operator confidence; less detail keeps chats quieter.",
            )
        )
        if self.io.confirm(self._t("是否修改过程反馈相关设置？", "Update channel progress feedback settings?"), default=False):
            send_progress_value = self.io.confirm(
                self._t("是否启用 channels.sendProgress？", "Enable channels.sendProgress?"),
                default=bool(send_progress),
            )
            send_tool_hints = read_binding(self.draft_config, SEND_TOOL_HINTS_FIELD)
            send_tool_hints_value = self.io.confirm(
                self._t("是否启用 channels.sendToolHints？", "Enable channels.sendToolHints?"),
                default=bool(send_tool_hints),
            )
            set_binding(self.draft_config, SEND_PROGRESS_FIELD, send_progress_value, clear_fallbacks=True)
            set_binding(self.draft_config, SEND_TOOL_HINTS_FIELD, send_tool_hints_value)

    def _show_memory_note(self) -> None:
        if "memory" in self.draft_config:
            self.io.write(
                self._t(
                    "检测到旧格式的 root memory 配置。\n注意：新的写出结果中将不再包含 root memory。\n如果需要，后续请改用当前支持的配置方式。",
                    "Old format detected: this config still has a root-level `memory` block.\n"
                    "Newly written config will omit root memory.\n"
                    "That keeps the saved file compatible with the current upstream loader, but it also means memory must be handled through the currently supported flow.",
                )
            )
        else:
            self.io.write(
                self._t(
                    "没有检测到显式 memory 配置。本向导默认不会新增 memory 配置。",
                    "No explicit memory configuration detected. This wizard does not create one by default, which keeps new writes conservative and upstream-compatible.",
                )
            )

    def _configure_security_preset(self) -> bool:
        default_choice = "1" if self.config_exists else "3"
        while True:
            options = security_preset_options(self.language)
            valid = {key for key, _ in options}
            self.io.write(self._render_security_preset_menu(default_choice))
            while True:
                choice = self.io.prompt(self._t("请输入选项：", "Select an option: ")).strip()
                if not choice:
                    choice = default_choice
                if choice in valid:
                    break
                self.io.write(self._t(f"无效选项：{choice}", f"Invalid choice: {choice}"))

            if choice == "1":
                return False
            if choice == "5":
                return True

            preset_key = {"2": "strict", "3": "balanced", "4": "open-dev"}[choice]
            preset = SECURITY_PRESETS[preset_key]

            if preset_key == "open-dev":
                self.io.write(
                    self._t(
                        "警告：你选择的是更开放的开发模式。\n这可能会降低安全性，仅建议用于本地调试或受控环境。",
                        "Warning: open-dev prioritizes convenience over isolation.\n"
                        "Use it only for local debugging, demos, or other controlled environments where broader access is acceptable.",
                    )
                )
                if not self.io.confirm(self._t("是否继续？", "Continue?"), default=False):
                    continue

            self.io.write(self._render_security_preset_preview(preset_key))
            if not self.io.confirm(self._t("是否应用这个预设？", "Apply this preset?"), default=True):
                continue

            set_binding(
                self.draft_config,
                TOOLS_RESTRICT_FIELD,
                preset.restrict_to_workspace,
                clear_fallbacks=True,
            )
            self.channel_security_defaults = {
                "allowFrom": list(preset.allow_from),
                "groupPolicy": preset.group_policy,
            }
            self.io.write(
                self._t(f"已应用安全预设：{self._preset_display_name(preset_key)}", f"Applied preset: {preset.slug}")
            )
            return False

    def _configure_channel_access_defaults(self) -> None:
        current_allow_from = self.channel_security_defaults["allowFrom"]
        action = self._field_action(
            label=self._t("默认渠道 allowFrom", "Default channel allowFrom"),
            current=current_allow_from,
            recommendation=self._t(
                "这些默认值只会用于本轮新增渠道，或你明确修改过的渠道。",
                "These defaults are used for channels you add or explicitly update in this wizard run.",
            ),
        )
        if action != "2":
            current_group_policy = self.channel_security_defaults["groupPolicy"]
        else:
            raw = self.io.ask_text(
                self._t(
                    "请输入默认 allowFrom，多个值用逗号分隔。留空表示显式空白名单。",
                    "Enter comma-separated default allowFrom entries. Leave blank for an explicit empty allow list.",
                ),
                default=",".join(current_allow_from) if current_allow_from else None,
                allow_empty=True,
            )
            values = parse_allow_from(raw)
            if values == ["*"] and not self._confirm_risky_value(
                field_name="allowFrom",
                value='["*"]',
                risk_summary=self._t("这会让实例对所有来源开放。", "This opens the instance to everyone."),
            ):
                self.io.write(
                    self._t("保持原有默认 allowFrom 设置。", "Keeping the previous default allowFrom value.")
                )
                values = current_allow_from
            self.channel_security_defaults["allowFrom"] = values
            current_group_policy = self.channel_security_defaults["groupPolicy"]

        action = self._field_action(
            label=self._t("默认渠道 groupPolicy", "Default channel groupPolicy"),
            current=current_group_policy,
            recommendation=self._t(
                "对 Telegram 和 Discord 来说，mention 是更稳妥的群聊默认值。",
                "Mention is the safest shared-group default for Telegram and Discord.",
            ),
        )
        if action != "2":
            return
        self.channel_security_defaults["groupPolicy"] = self._prompt_group_policy(
            current_group_policy,
            scope_name=self._t("默认渠道 groupPolicy", "default channel groupPolicy"),
        )

    def _configure_restrict_to_workspace(self) -> None:
        current = bool(read_binding(self.draft_config, TOOLS_RESTRICT_FIELD))
        action = self._field_action(
            label="tools.restrictToWorkspace",
            current=current,
            recommendation=self._t(
                "生产环境和多实例环境建议保持开启。",
                "Keep this enabled in production and multi-instance setups.",
            ),
        )
        if action != "2":
            return

        value = self.io.confirm(
            self._t("是否启用 tools.restrictToWorkspace？", "Enable tools.restrictToWorkspace?"),
            default=current,
        )
        if not value:
            self.io.write(
                self._t(
                    "警告：tools.restrictToWorkspace = false 会放宽到当前工作目录之外的文件访问。",
                    "Warning: tools.restrictToWorkspace = false broadens file access beyond the current workspace.",
                )
            )
            if not self._confirm_risky_value(
                field_name="tools.restrictToWorkspace",
                value="false",
                risk_summary=self._t(
                    "这会让实例访问工作目录之外的文件，安全边界会变宽。",
                    "This broadens file access beyond the configured workspace.",
                ),
            ):
                self.io.write(
                    self._t("保持 tools.restrictToWorkspace = true。", "Keeping tools.restrictToWorkspace enabled.")
                )
                value = True

        set_binding(self.draft_config, TOOLS_RESTRICT_FIELD, value, clear_fallbacks=True)

    def _configure_telegram(self) -> bool:
        telegram_channel = ensure_channel_defaults(self.draft_config, "telegram")

        if self.telegram_connected_at_start:
            self.io.write(
                self._t(
                    "检测到当前实例已启用 Telegram。",
                    "Detected an existing Telegram connection on this instance.",
                )
            )
            self.io.write("")
            self.io.write(self._t("当前状态：", "Current status:"))
            self.io.write(f"- Telegram{self._t('：', ': ')}{self._t('已启用', 'enabled')}")
            self.io.write(
                f"- Token{self._t('：', ': ')}"
                f"{self._t(f'已配置（{mask_secret(telegram_channel.get('token'))}）', f'configured ({mask_secret(telegram_channel.get('token'))})')}"
            )
            self.io.write(f"- allowFrom{self._t('：', ': ')}{compact_json_value(telegram_channel.get('allowFrom', []))}")
            self.io.write(f"- groupPolicy{self._t('：', ': ')}{telegram_channel.get('groupPolicy', 'mention')}")
            self.io.write(f"- {self._t('工作目录', 'Workspace')}{self._t('：', ': ')}{read_binding(self.draft_config, WORKSPACE_FIELD) or self._t('未设置', '(unset)')}")
            self.io.write(
                f"- {self._t('网关端口', 'Gateway port')}{self._t('：', ': ')}"
                f"{read_binding(self.draft_config, GATEWAY_PORT_FIELD) or self._t('未设置', '(unset)')}"
            )
            choice = self.io.choose(
                self._t(
                    "请选择你想进行的操作：\n\n说明：\n- 默认不会要求你重新填写 Telegram token\n- 如果只想继续看其他配置，选 [1] 或 [4]",
                    "Existing Telegram maintenance mode\n\n"
                    "This instance already has a working Telegram bot.\n"
                    "The default action is to keep it unchanged so you can safely review other settings without re-entering the token.\n"
                    "Choose another option only if you want to rotate credentials, tighten access, or split Telegram into its own instance.",
                ),
                telegram_maintenance_options(self.language),
                default="1",
            )

            if choice == "2":
                token = self.io.prompt_secret(self._t("请输入新的 Telegram token：", "Enter the new Telegram token: "))
                ensure_channel_defaults(self.draft_config, "telegram")
                set_path(self.draft_config, ("channels", "telegram", "enabled"), True)
                set_path(self.draft_config, ("channels", "telegram", "token"), token)
            elif choice == "7":
                self._configure_channel_access("telegram")
            elif choice == "3":
                if self.io.confirm(
                    self._t("是否在当前实例中暂时禁用 Telegram？", "Disable Telegram on the current instance?"),
                    default=False,
                ):
                    set_path(self.draft_config, ("channels", "telegram", "enabled"), False)
            elif choice == "5":
                self._configure_split_plan()
                return self.io.confirm(
                    self._t("是否也继续检查当前实例中的 Discord / Feishu？", "Review Discord / Feishu on the current instance as well?"),
                    default=False,
                )
            elif choice == "4":
                return True
            elif choice == "6":
                self._show_telegram_safety_review()
                return self.io.confirm(
                    self._t("是否继续检查当前实例中的 Discord / Feishu？", "Review Discord / Feishu on this instance?"),
                    default=False,
                )

            return self.io.confirm(
                self._t("是否继续检查当前实例中的 Discord / Feishu？", "Review Discord / Feishu on this instance?"),
                default=False,
            )

        choice = self.io.choose(
            self._t("当前实例尚未配置 Telegram。", "Telegram is not configured on this instance."),
            telegram_new_options(self.language),
            default="1",
        )
        if choice == "2":
            ensure_channel_defaults(self.draft_config, "telegram")
            token = self.io.prompt_secret(self._t("请输入 Telegram token：", "Enter the Telegram token: "))
            set_path(self.draft_config, ("channels", "telegram", "enabled"), True)
            set_path(self.draft_config, ("channels", "telegram", "token"), token)
            set_path(
                self.draft_config,
                ("channels", "telegram", "allowFrom"),
                list(self.channel_security_defaults["allowFrom"]),
            )
            set_path(
                self.draft_config,
                ("channels", "telegram", "groupPolicy"),
                self.channel_security_defaults["groupPolicy"],
            )

        return self.io.confirm(
            self._t("是否继续检查当前实例中的 Discord / Feishu？", "Review Discord / Feishu on this instance?"),
            default=False,
        )

    def _configure_discord(self) -> None:
        channel_path = ("channels", "discord")
        channel = ensure_channel_defaults(self.draft_config, "discord")
        configured = detect_channel_enabled(self.original_config, "discord")

        if configured:
            self.io.write(self._t("Discord：已配置。", "Discord status: already configured."))
            self.io.write(
                f"- Token{self._t('：', ': ')}"
                f"{self._t(f'已配置（{mask_secret(channel.get('token'))}）', f'configured ({mask_secret(channel.get('token'))})')}"
            )
            self.io.write(f"- allowFrom{self._t('：', ': ')}{compact_json_value(channel.get('allowFrom', []))}")
            self.io.write(f"- groupPolicy{self._t('：', ': ')}{channel.get('groupPolicy', 'mention')}")
            choice = self.io.choose(
                self._t("请选择 Discord 的处理方式：", "Discord maintenance"),
                [
                    ("1", self._t("保持 Discord 当前状态不变", "Keep Discord unchanged")),
                    ("2", self._t("更新 Discord token", "Update Discord token")),
                    ("3", self._t("修改 Discord 访问控制", "Modify Discord access")),
                    ("4", self._t("禁用 Discord", "Disable Discord")),
                ],
                default="1",
            )
            if choice == "2":
                token = self.io.prompt_secret(self._t("请输入新的 Discord token：", "Enter the new Discord token: "))
                set_path(self.draft_config, channel_path + ("enabled",), True)
                set_path(self.draft_config, channel_path + ("token",), token)
            elif choice == "3":
                self._configure_channel_access("discord")
            elif choice == "4":
                if self.io.confirm(
                    self._t("是否在当前实例中禁用 Discord？", "Disable Discord on the current instance?"),
                    default=False,
                ):
                    set_path(self.draft_config, channel_path + ("enabled",), False)
            return

        choice = self.io.choose(
            self._t("Discord：未配置。", "Discord is not configured."),
            [
                ("1", self._t("保持未配置", "Leave Discord unconfigured")),
                ("2", self._t("新增 Discord", "Add Discord")),
            ],
            default="1",
        )
        if choice != "2":
            return

        token = self.io.prompt_secret(self._t("请输入 Discord token：", "Enter the Discord token: "))
        ensure_channel_defaults(self.draft_config, "discord")
        set_path(self.draft_config, channel_path + ("enabled",), True)
        set_path(self.draft_config, channel_path + ("token",), token)
        self._configure_channel_access("discord", is_new_channel=True)

    def _configure_feishu(self) -> None:
        channel_path = ("channels", "feishu")
        channel = ensure_channel_defaults(self.draft_config, "feishu")
        configured = detect_channel_enabled(self.original_config, "feishu")

        if configured:
            self.io.write(self._t("Feishu：已配置。", "Feishu status: already configured."))
            self.io.write(f"- appId{self._t('：', ': ')}{channel.get('appId', self._t('未设置', '(unset)'))}")
            self.io.write(
                f"- appSecret{self._t('：', ': ')}"
                f"{self._t(f'已配置（{mask_secret(channel.get('appSecret'))}）', f'configured ({mask_secret(channel.get('appSecret'))})')}"
            )
            self.io.write(f"- allowFrom{self._t('：', ': ')}{compact_json_value(channel.get('allowFrom', []))}")
            choice = self.io.choose(
                self._t("请选择 Feishu 的处理方式：", "Feishu maintenance"),
                [
                    ("1", self._t("保持 Feishu 当前状态不变", "Keep Feishu unchanged")),
                    ("2", self._t("更新 Feishu 凭据", "Update Feishu credentials")),
                    ("3", self._t("修改 Feishu 访问控制", "Modify Feishu access")),
                    ("4", self._t("禁用 Feishu", "Disable Feishu")),
                ],
                default="1",
            )
            if choice == "2":
                self._configure_feishu_credentials()
            elif choice == "3":
                self._configure_channel_access("feishu")
            elif choice == "4":
                if self.io.confirm(
                    self._t("是否在当前实例中禁用 Feishu？", "Disable Feishu on the current instance?"),
                    default=False,
                ):
                    set_path(self.draft_config, channel_path + ("enabled",), False)
            return

        choice = self.io.choose(
            self._t("Feishu：未配置。", "Feishu is not configured."),
            [
                ("1", self._t("保持未配置", "Leave Feishu unconfigured")),
                ("2", self._t("新增 Feishu", "Add Feishu")),
            ],
            default="1",
        )
        if choice != "2":
            return

        self._configure_feishu_credentials()
        set_path(self.draft_config, channel_path + ("enabled",), True)
        self._configure_channel_access("feishu", is_new_channel=True)

    def _configure_feishu_credentials(self) -> None:
        channel_path = ("channels", "feishu")
        ensure_channel_defaults(self.draft_config, "feishu")
        current_app_id = get_path(self.draft_config, channel_path + ("appId",))
        app_id = self.io.ask_text(self._t("请输入 Feishu appId", "Enter the Feishu appId"), default=current_app_id)
        app_secret = self.io.prompt_secret(self._t("请输入 Feishu appSecret：", "Enter the Feishu appSecret: "))
        encrypt_key = self.io.ask_text(
            self._t("请输入 Feishu encryptKey（可选）", "Enter the Feishu encryptKey (optional)"),
            default=get_path(self.draft_config, channel_path + ("encryptKey",)),
            allow_empty=True,
        )
        verification_token = self.io.ask_text(
            self._t("请输入 Feishu verificationToken（可选）", "Enter the Feishu verificationToken (optional)"),
            default=get_path(self.draft_config, channel_path + ("verificationToken",)),
            allow_empty=True,
        )

        set_path(self.draft_config, channel_path + ("enabled",), True)
        set_path(self.draft_config, channel_path + ("appId",), app_id)
        set_path(self.draft_config, channel_path + ("appSecret",), app_secret)
        if encrypt_key:
            set_path(self.draft_config, channel_path + ("encryptKey",), encrypt_key)
        if verification_token:
            set_path(self.draft_config, channel_path + ("verificationToken",), verification_token)

    def _configure_channel_access(self, channel_name: str, *, is_new_channel: bool = False) -> None:
        channel_path = ("channels", channel_name)
        channel = ensure_channel_defaults(self.draft_config, channel_name)

        for field_name in CHANNEL_ACCESS_FIELDS[channel_name]:
            if field_name == "allowFrom":
                current = channel.get("allowFrom", self.channel_security_defaults["allowFrom"])
                if is_new_channel:
                    raw = self.io.ask_text(
                        self._t(
                            f"请输入 {channel_name} 的 allowFrom，多个值用逗号分隔。留空表示显式空白名单。",
                            f"Enter comma-separated {channel_name} allowFrom entries. Leave blank for an explicit empty allow list.",
                        ),
                        default=",".join(current) if current else None,
                        allow_empty=True,
                    )
                    values = parse_allow_from(raw)
                    if values == ["*"] and not self._confirm_risky_value(
                        field_name=f"{channel_name}.allowFrom",
                        value='["*"]',
                        risk_summary=self._t("这会让该渠道对所有来源开放。", "This opens the channel to everyone."),
                    ):
                        values = current
                    set_path(self.draft_config, channel_path + ("allowFrom",), values)
                else:
                    self._configure_nested_allow_from(channel_name)
            elif field_name == "groupPolicy":
                current = channel.get("groupPolicy", self.channel_security_defaults["groupPolicy"])
                if is_new_channel:
                    value = self._prompt_group_policy(current, scope_name=f"{channel_name} groupPolicy")
                    set_path(self.draft_config, channel_path + ("groupPolicy",), value)
                else:
                    self._configure_nested_group_policy(channel_name)

    def _configure_nested_allow_from(self, channel_name: str) -> None:
        channel_path = ("channels", channel_name, "allowFrom")
        current = get_path(
            self.draft_config,
            channel_path,
            self.channel_security_defaults["allowFrom"],
        )
        action = self._field_action(
            label=f"{channel_name} allowFrom",
            current=current,
            recommendation=self._t(
                "建议保持显式白名单。除非是本地开发环境，否则不要使用 [*]。",
                "Keep channel allow lists explicit. Avoid [*] unless you are on a local dev box.",
            ),
        )
        if action != "2":
            return

        raw = self.io.ask_text(
            self._t(
                f"请输入 {channel_name} 的 allowFrom，多个值用逗号分隔。留空表示显式空白名单。",
                f"Enter comma-separated {channel_name} allowFrom entries. Leave blank for an explicit empty allow list.",
            ),
            default=",".join(current) if current else None,
            allow_empty=True,
        )
        values = parse_allow_from(raw)
        if values == ["*"] and not self._confirm_risky_value(
            field_name=f"{channel_name}.allowFrom",
            value='["*"]',
            risk_summary=self._t("这会让该渠道对所有来源开放。", "This opens the channel to everyone."),
        ):
            self.io.write(
                self._t(f"保持原有 {channel_name} allowFrom 值。", f"Keeping the previous {channel_name} allowFrom value.")
            )
            return
        set_path(self.draft_config, channel_path, values)

    def _configure_nested_group_policy(self, channel_name: str) -> None:
        channel_path = ("channels", channel_name, "groupPolicy")
        current = get_path(
            self.draft_config,
            channel_path,
            self.channel_security_defaults["groupPolicy"],
        )
        action = self._field_action(
            label=f"{channel_name} groupPolicy",
            current=current,
            recommendation=self._t(
                "群聊默认建议使用 mention，更稳妥。",
                "Mention-only remains the safest shared-group default.",
            ),
        )
        if action != "2":
            return

        value = self._prompt_group_policy(current, scope_name=f"{channel_name} groupPolicy")
        set_path(self.draft_config, channel_path, value)

    def _prompt_group_policy(self, current: str, *, scope_name: str) -> str:
        choice = self.io.choose(
            self._t(f"请选择 {scope_name}", f"Choose {scope_name}"),
            group_policy_options(self.language),
            default="1" if current == "mention" else "3",
        )

        if choice == "1":
            return "mention"
        if choice == "2":
            if not self._confirm_risky_value(
                field_name=scope_name,
                value="open",
                risk_summary=self._t("这会让群聊访问策略更开放。", "This makes the group access policy more open."),
            ):
                return current
            return "open"

        value = self.io.ask_text(self._t(f"请输入 {scope_name}", f"Enter {scope_name}"), default=current)
        if value == "open" and not self._confirm_risky_value(
            field_name=scope_name,
            value="open",
            risk_summary=self._t("这会让群聊访问策略更开放。", "This makes the group access policy more open."),
        ):
            return current
        return value

    def _show_telegram_safety_review(self) -> None:
        notes = build_telegram_safety_notes(self.draft_config, self.language)
        self.io.write(self._t("当前 Telegram 实例安全检查：", "Telegram safety review:"))
        for note in notes:
            self.io.write(f"- {note}")

    def _configure_split_plan(self) -> None:
        self.io.write(
            self._t(
                "复制为独立 Telegram 实例有助于隔离工作目录、运行时目录和端口，也更适合把 Telegram 单独维护。\n代价是你需要多维护一个配置文件和运行进程。",
                "A dedicated Telegram instance is useful when you want cleaner isolation for workspace files, runtime artifacts, and ports.\n"
                "It also makes Telegram-specific maintenance and access rules easier to reason about.\n"
                "The trade-off is that you will manage one more config file and usually one more running process.",
            )
        )
        self.io.write("")
        suggested_path = suggest_split_config_path(self.config_path)
        target_text = self.io.ask_text(
            self._t("请输入独立 Telegram 实例的配置文件路径", "Enter the standalone Telegram config path"),
            default=str(suggested_path),
        )
        target_path = Path(target_text).expanduser()
        suggested_workspace = default_instance_workspace(target_path)
        current_port = read_binding(self.draft_config, GATEWAY_PORT_FIELD) or DEFAULT_GATEWAY_PORT
        suggested_port = suggest_gateway_port(current_port)

        workspace = self.io.ask_text(
            self._t("请输入独立 Telegram 实例的工作目录", "Enter the standalone workspace path"),
            default=suggested_workspace,
        )
        port_raw = self.io.ask_text(
            self._t("请输入独立 Telegram 实例的网关端口", "Enter the standalone gateway port"),
            default=str(suggested_port),
        )
        port = int(port_raw)
        pure_telegram = self.io.confirm(
            self._t("是否将新实例设置为纯 Telegram 实例？", "Make the new instance Telegram-only?"),
            default=True,
        )
        copy_search = self.io.confirm(
            self._t("是否复制当前 web search 设置到新实例？", "Copy the current web search settings to the new instance?"),
            default=True,
        )
        disable_original = self.io.confirm(
            self._t("创建完成后，是否在当前实例中禁用 Telegram？", "Disable Telegram on the current instance after creating the new one?"),
            default=False,
        )

        split_draft = deep_copy_config(self.draft_config)
        set_binding(split_draft, WORKSPACE_FIELD, workspace, clear_fallbacks=True)
        set_binding(split_draft, GATEWAY_PORT_FIELD, port)

        if pure_telegram:
            telegram_config = get_path(split_draft, ("channels", "telegram"), {}) or {}
            split_draft["channels"] = {
                "sendProgress": bool(read_binding(split_draft, SEND_PROGRESS_FIELD)),
                "sendToolHints": bool(read_binding(split_draft, SEND_TOOL_HINTS_FIELD)),
                "telegram": telegram_config,
            }

        if not copy_search:
            delete_path(split_draft, ("tools", "web", "search"))

        if disable_original:
            set_path(self.draft_config, ("channels", "telegram", "enabled"), False)

        next_command = f"nanobot gateway --config {target_path}"
        self.split_plan = SplitPlan(
            config_path=target_path,
            draft_config=split_draft,
            next_command=next_command,
            pure_telegram=pure_telegram,
            copy_search=copy_search,
        )

    def _preset_display_name(self, preset_key: str) -> str:
        names = {
            "strict": self._t("严格模式", "strict"),
            "balanced": self._t("平衡模式", "balanced"),
            "open-dev": self._t("开发开放模式", "open-dev"),
        }
        return names[preset_key]

    def _render_security_preset_menu(self, default_choice: str) -> str:
        if self.is_zh:
            return "\n".join(
                [
                    "请选择一个安全预设。",
                    "",
                    "推荐：[3] 平衡模式",
                    f"当前默认：[{'1' if default_choice == '1' else default_choice}] "
                    f"{'保持当前值' if default_choice == '1' else '平衡模式' if default_choice == '3' else '严格模式' if default_choice == '2' else '开发开放模式' if default_choice == '4' else '手动逐项检查'}",
                    "",
                    "[1] 保持当前值",
                    "    保持当前安全相关配置不变，快速跳过本轮。",
                    "",
                    "[2] 严格模式",
                    "    更适合个人长期使用或生产环境，默认更保守。",
                    "    预览：",
                    "    - tools.restrictToWorkspace = true",
                    "    - groupPolicy = mention",
                    "    - allowFrom 保持当前值，或要求明确白名单",
                    "    - 不启用开放访问",
                    "",
                    "[3] 平衡模式",
                    "    适合大多数用户，推荐默认选择。",
                    "    预览：",
                    "    - tools.restrictToWorkspace = true",
                    "    - groupPolicy = mention",
                    "    - allowFrom 尽量保持当前值，除非明显过宽",
                    "    - 在安全和可用性之间折中",
                    "",
                    "[4] 开发开放模式",
                    "    更适合本地调试，使用更方便，但安全性更低。",
                    "    预览：",
                    "    - 可能允许更宽的访问范围",
                    "    - 可能使用 groupPolicy = open",
                    "    - 需要额外确认",
                    "",
                    "[5] 不使用预设，手动逐项检查",
                    "    不直接套用预设，而是继续逐项查看安全字段。",
                ]
            )
        return "\n".join(
            [
                "Choose a security preset.",
                "",
                "Recommended: [3] balanced",
                "Why presets exist: they let you apply a coherent safety posture without guessing how each access field interacts.",
                "",
                "[1] Keep current values",
                "    Best when this instance already works and you do not want this round to change access behavior.",
                "    Benefit: zero surprise changes.",
                "    Trade-off: you also keep any risky choices that may already exist.",
                "",
                "[2] strict",
                "    Best for long-running personal or production use where safety matters more than convenience.",
                "    Benefit: stronger workspace isolation and more conservative group behavior.",
                "    Trade-off: you may need explicit allow lists and a bit more manual setup.",
                "",
                "[3] balanced",
                "    Recommended for most users who want safer defaults without too much friction.",
                "    Benefit: protects workspace access while keeping normal day-to-day use comfortable.",
                "    Trade-off: still expects you to be intentional about broad access rules.",
                "",
                "[4] open-dev",
                "    Best for local debugging when speed matters more than isolation.",
                "    Benefit: faster testing and fewer access barriers during development.",
                "    Trade-off: broader access and looser group behavior, so it is not a good production default.",
                "",
                "[5] Skip preset and review fields manually",
                "    Use this when you want full control over each field instead of a preset bundle.",
                "    Benefit: precise control.",
                "    Trade-off: slower and easier to misconfigure if you are unsure about the fields.",
                "",
                f"Default: [{default_choice}]",
            ]
        )

    def _render_security_preset_preview(self, preset_key: str) -> str:
        preset = SECURITY_PRESETS[preset_key]
        allow_preview = self._t(
            "保持当前值，或要求明确白名单" if preset_key == "strict" else
            "尽量保持当前值，除非明显过宽" if preset_key == "balanced" else
            '允许更宽的访问范围（例如 ["*"]）',
            "keep current value or require an explicit allow list" if preset_key == "strict" else
            "keep the current value unless it is obviously too broad" if preset_key == "balanced" else
            'broader access may be used (for example ["*"])',
        )
        other_summary = self._t(
            "未明确列出的字段将保持当前值",
            "Fields not listed below will keep their current values.",
        )
        benefit = self._t(
            "",
            "protect the workspace boundary and keep shared-channel behavior conservative"
            if preset_key in {"strict", "balanced"}
            else "remove friction during local debugging and short-lived testing",
        )
        tradeoff = self._t(
            "",
            "more manual allow-list work and slightly stricter day-to-day behavior"
            if preset_key == "strict"
            else "some convenience remains limited to avoid broad access by default"
            if preset_key == "balanced"
            else "less isolation, broader access, and a higher chance of accidental exposure",
        )
        return "\n".join(
            [
                self._t(f"你选择了：{self._preset_display_name(preset_key)}", f"You chose: {preset.slug}"),
                "",
                self._t("", "Why people choose this preset:"),
                self._t("", f"- {benefit}"),
                self._t("", "Trade-off:"),
                self._t("", f"- {tradeoff}"),
                self._t("", ""),
                self._t("本预设将处理以下字段：", "This preset will update:"),
                f"- tools.restrictToWorkspace{self._t('：', ': ')}{preset.restrict_to_workspace}",
                f"- groupPolicy{self._t('：', ': ')}{preset.group_policy}",
                f"- allowFrom{self._t('：', ': ')}{allow_preview}",
                f"- {self._t('其他相关字段', 'Other related fields')}{self._t('：', ': ')}{other_summary}",
                "",
                self._t(
                    "说明：\n- 未明确列出的字段将保持当前值\n- 你仍然可以在后续摘要中看到最终改动",
                    "Notes:\n- Fields not listed here keep their current values.\n- This preview exists so you can understand the bundle before applying it.\n- You can still review the final diff in the summary.",
                ),
            ]
        )

    def _confirm_risky_value(self, *, field_name: str, value: str, risk_summary: str) -> bool:
        message = "\n".join(
            [
                self._t("警告：你即将启用较高风险的配置：", "Warning: you are about to enable a higher-risk configuration:"),
                f"- {field_name} = {value}",
                "",
                self._t("这可能带来的影响：", "Potential impact:"),
                f"- {risk_summary}",
                "",
                self._t("建议用于以下场景：", "Recommended only for:"),
                f"- {self._t('本地调试', 'Local debugging')}",
                f"- {self._t('临时测试', 'Temporary testing')}",
                f"- {self._t('明确受控的开发环境', 'Explicitly controlled development environments')}",
                "",
                self._t("不建议用于：", "Not recommended for:"),
                f"- {self._t('长期运行实例', 'Long-running instances')}",
                f"- {self._t('对外开放环境', 'Public-facing environments')}",
                f"- {self._t('含敏感配置的环境', 'Environments with sensitive configuration')}",
                "",
                self._t(
                    "是否继续？",
                    "Continue?\nThis setting can be useful in controlled development environments, but it lowers your safety margin.",
                ),
            ]
        )
        return self.io.confirm(message, default=False)

    def _field_action(self, *, label: str, current: Any, recommendation: str) -> str:
        display = compact_json_value(current) if current is not None else self._t("未设置", "(unset)")
        return self.io.choose(
            self._t(
                f"{label}\n当前值：{display}\n建议：{recommendation}",
                f"{label}\nCurrent value: {display}\nWhy this matters: {recommendation}",
            ),
            keep_modify_skip_options(self.language),
            default="1",
        )

    def _render_split_summary(self) -> str | None:
        if self.split_plan is None:
            return None

        runtime_layout = derived_runtime_paths(self.split_plan.config_path)
        channel_names = ", ".join(
            name for name in sorted(self.split_plan.draft_config.get("channels", {}))
            if name not in {"sendProgress", "sendToolHints"}
        )
        return "\n".join(
            [
                self._t(
                    f"独立 Telegram 实例计划：{self.split_plan.config_path}",
                    f"Dedicated Telegram instance plan for {self.split_plan.config_path}",
                ),
                self._t(
                    "- 这样做的好处：Telegram 可以拥有独立的目录、端口和运行边界。",
                    "- Why this can help: Telegram gets its own workspace, runtime layout, and port boundary.",
                ),
                self._t(
                    "- 代价：你需要额外管理一个配置文件和实例进程。",
                    "- Trade-off: you will manage one more config file and one more instance process.",
                ),
                f"- {self._t('工作目录', 'Workspace')}{self._t('：', ': ')}{read_binding(self.split_plan.draft_config, WORKSPACE_FIELD) or self._t('未设置', '(unset)')}",
                f"- {self._t('网关端口', 'Gateway port')}{self._t('：', ': ')}{read_binding(self.split_plan.draft_config, GATEWAY_PORT_FIELD) or self._t('未设置', '(unset)')}",
                f"- {self._t('Runtime 目录', 'Runtime dir')}{self._t('：', ': ')}{runtime_layout['runtime']}",
                f"- {self._t('Media 目录', 'Media dir')}{self._t('：', ': ')}{runtime_layout['media']}",
                f"- {self._t('Cron 目录', 'Cron dir')}{self._t('：', ': ')}{runtime_layout['cron']}",
                f"- {self._t('新实例包含的渠道', 'Channels in the new instance')}{self._t('：', ': ')}{channel_names or self._t('未配置', '(none)')}",
                f"- {self._t('纯 Telegram 实例', 'Telegram-only')}{self._t('：', ': ')}{self.split_plan.pure_telegram}",
                f"- {self._t('复制 web search', 'Copy web search')}{self._t('：', ': ')}{self.split_plan.copy_search}",
                self._t(
                    "- 新写出的配置中将不再包含 root memory",
                    "- Newly written config will omit root memory",
                ),
                f"- {self._t('下一步命令', 'Next command to start the dedicated instance')}{self._t('：', ': ')}{self.split_plan.next_command}",
            ]
        )


def resolve_config_path(config_arg: str | None) -> Path:
    if config_arg:
        return Path(config_arg).expanduser()
    return Path.home() / ".nanobot" / "config.json"


def default_instance_workspace(config_path: Path) -> str:
    return str(config_path.expanduser().parent / "workspace")


def derived_runtime_paths(config_path: Path) -> dict[str, str]:
    base_dir = config_path.expanduser().parent
    return {
        "runtime": str(base_dir / "runtime"),
        "media": str(base_dir / "media"),
        "cron": str(base_dir / "cron"),
    }


def suggest_split_config_path(config_path: Path) -> Path:
    base_dir = config_path.expanduser().parent
    if base_dir.name == ".nanobot":
        new_dir = base_dir.with_name(".nanobot-telegram")
    elif base_dir.name.endswith("-telegram"):
        new_dir = base_dir.with_name(f"{base_dir.name}-copy")
    else:
        new_dir = base_dir.with_name(f"{base_dir.name}-telegram")
    return new_dir / config_path.name


def suggest_gateway_port(current_port: Any) -> int:
    try:
        port = int(current_port)
    except (TypeError, ValueError):
        return DEFAULT_GATEWAY_PORT + 1
    return port + 1


def detect_instance(config_path: Path, config: dict[str, Any], language: str = "en") -> InstanceInfo:
    enabled_channels = [name for name in CHANNEL_ORDER if detect_channel_enabled(config, name)]
    return InstanceInfo(
        config_path=config_path,
        workspace=read_binding(config, WORKSPACE_FIELD),
        gateway_port=read_binding(config, GATEWAY_PORT_FIELD),
        enabled_channels=enabled_channels,
        web_search_provider=detect_web_search_status(config, language),
        telegram_connected=detect_telegram_connected(config),
    )


def detect_telegram_connected(config: dict[str, Any]) -> bool:
    telegram = get_path(config, ("channels", "telegram"), {}) or {}
    return bool(telegram.get("enabled") and telegram.get("token"))


def detect_channel_enabled(config: dict[str, Any], channel_name: str) -> bool:
    channel = get_path(config, ("channels", channel_name), {}) or {}
    return bool(channel.get("enabled"))


def ensure_channel_defaults(config: dict[str, Any], channel_name: str) -> dict[str, Any]:
    channel = get_path(config, ("channels", channel_name))
    if not isinstance(channel, dict):
        channel = {}

    merged = dict(CHANNEL_DEFAULT_CONFIGS[channel_name])
    merged.update(channel)
    set_path(config, ("channels", channel_name), merged)
    return merged


def detect_channel_security_defaults(config: dict[str, Any]) -> dict[str, Any]:
    for channel_name in CHANNEL_ORDER:
        channel = get_path(config, ("channels", channel_name), {}) or {}
        if not isinstance(channel, dict):
            continue
        if "allowFrom" in channel or "groupPolicy" in channel:
            return {
                "allowFrom": list(channel.get("allowFrom", DEFAULT_CHANNEL_ALLOW_FROM)),
                "groupPolicy": channel.get("groupPolicy", DEFAULT_CHANNEL_GROUP_POLICY),
            }

    return {
        "allowFrom": list(DEFAULT_CHANNEL_ALLOW_FROM),
        "groupPolicy": DEFAULT_CHANNEL_GROUP_POLICY,
    }


def parse_allow_from(raw: str) -> list[str]:
    if not raw.strip():
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_telegram_safety_notes(config: dict[str, Any], language: str = "en") -> list[str]:
    is_zh = language == "zh"
    notes: list[str] = []
    if not bool(read_binding(config, TOOLS_RESTRICT_FIELD)):
        notes.append(
            "tools.restrictToWorkspace 已关闭。生产环境建议开启。"
            if is_zh
            else "tools.restrictToWorkspace is disabled. Enable it for safer production use."
        )
    if not read_binding(config, WORKSPACE_FIELD):
        notes.append(
            "workspace 未设置。建议为每个实例指定明确的工作目录。"
            if is_zh
            else "workspace is not set. Use an explicit workspace per instance."
        )

    telegram = get_path(config, ("channels", "telegram"), {}) or {}
    allow_from = telegram.get("allowFrom", [])
    if allow_from == ["*"]:
        notes.append(
            "Telegram allowFrom = [*] 过宽。"
            if is_zh
            else "Telegram allowFrom = [*] is overly broad."
        )
    elif not allow_from:
        notes.append(
            "Telegram allowFrom 为空。只有在上层网关还有额外过滤时才建议保持为空。"
            if is_zh
            else "Telegram allowFrom is empty. Keep it that way only if a higher-level gateway filter exists."
        )

    if telegram.get("groupPolicy", "mention") == "open":
        notes.append(
            "Telegram groupPolicy = open 在群聊场景下风险较高。"
            if is_zh
            else "Telegram groupPolicy = open is risky in shared groups."
        )

    enabled_channels = [name for name in CHANNEL_ORDER if detect_channel_enabled(config, name)]
    if len(enabled_channels) > 1:
        notes.append(
            "当前实例混合了多个渠道。可以考虑把 Telegram 拆成独立实例。"
            if is_zh
            else "Multiple channels share this instance. Consider splitting Telegram into its own config."
        )

    if not read_binding(config, SEARCH_API_KEY_FIELD):
        notes.append("Brave Search API key 未配置。" if is_zh else "Brave Search API key is not configured.")
    if "memory" not in config:
        notes.append("没有检测到显式 memory 配置。" if is_zh else "No explicit memory block detected.")

    gateway_port = read_binding(config, GATEWAY_PORT_FIELD)
    if gateway_port is not None:
        notes.append(
            f"请确认 gateway.port {gateway_port} 不会与其他运行中的实例冲突。"
            if is_zh
            else f"Verify gateway.port {gateway_port} does not conflict with another running instance."
        )

    if not notes:
        notes.append(
            "当前 Telegram 实例已通过这轮安全检查。"
            if is_zh
            else "This Telegram instance already matches the current safety checks."
        )
    return notes


def detect_web_search_status(config: dict[str, Any], language: str = "en") -> str:
    api_key = read_binding(config, SEARCH_API_KEY_FIELD)
    max_results = read_binding(config, SEARCH_MAX_RESULTS_FIELD) or DEFAULT_SEARCH_MAX_RESULTS
    is_zh = language == "zh"
    if api_key:
        if is_zh:
            return f"Brave Search 已配置（{mask_secret(api_key)}），maxResults = {max_results}"
        return f"Brave Search API configured ({mask_secret(api_key)}), maxResults = {max_results}"
    if is_zh:
        return f"Brave Search 未配置，maxResults = {max_results}"
    return f"Brave Search API not configured, maxResults = {max_results}"


def localized_skill_title(skill: dict[str, str], language: str) -> str:
    if language != "zh":
        return skill["title"]
    titles = {
        "workspace-guard": "Workspace Guard",
        "telegram-ops": "Telegram Ops",
        "search-defaults": "Search Defaults",
    }
    return titles.get(skill["slug"], skill["title"])


def localized_skill_description(skill: dict[str, str], language: str) -> str:
    if language != "zh":
        return skill["description"]
    descriptions = {
        "workspace-guard": "为本地文件操作提供更严格的工作区保护和审计建议",
        "telegram-ops": "提供 Telegram 机器人维护建议，例如健康检查和 token 轮换",
        "search-defaults": "为 nanoBot 提供更稳妥的搜索默认配置和约束建议",
    }
    return descriptions.get(skill["slug"], skill["description"])


def build_legacy_handling_notes(
    *,
    primary_rewrites: list[str],
    split_rewrites: list[str],
    primary_memory_removed: bool,
    split_memory_removed: bool,
    primary_will_write: bool,
    has_split: bool,
    current_config_has_memory: bool,
    language: str = "en",
) -> list[str]:
    is_zh = language == "zh"
    notes: list[str] = []
    if primary_rewrites:
        notes.append(
            "检测到旧格式字段，当前配置会改写为规范路径："
            + ", ".join(primary_rewrites)
            if is_zh
            else "Old format detected: this config still uses legacy field paths. "
            "The wizard will normalize them to the current canonical structure when saving: "
            + ", ".join(primary_rewrites)
        )
    if split_rewrites:
        notes.append(
            "独立实例输出会改写旧格式字段为规范路径："
            + ", ".join(split_rewrites)
            if is_zh
            else "Old format detected in the split output plan. "
            "The saved dedicated config will normalize those fields to the current canonical structure: "
            + ", ".join(split_rewrites)
        )

    if primary_memory_removed:
        notes.append(
            "新写出的配置中将不再包含 root memory。"
            if is_zh
            else "Old format detected: root memory is present in the current config. "
            "Newly written config will omit root memory. "
            "That keeps the saved file compatible with the current upstream schema, which rejects root-level memory."
        )
    elif split_memory_removed:
        notes.append(
            "独立实例写出结果中将不再包含 root memory。"
            if is_zh
            else "Old format detected in the split output plan: root memory is present. "
            "Newly written config will omit root memory in the dedicated Telegram output as well, "
            "so the split instance stays compatible with the current upstream schema."
        )
    elif current_config_has_memory and not primary_will_write and not has_split:
        notes.append(
            "本次没有写盘，所以当前文件里的 root memory 会原样保留；后续一旦重新写出，新配置中将不再包含 root memory。"
            if is_zh
            else "Old format detected: root memory is still present in the current file because this run does not write anything. "
            "Any future saved config will omit it so the result stays loadable."
        )
    return notes


def run_wizard(config_path: Path, io: WizardIO | None = None) -> WizardResult:
    runner = Wizard(config_path=config_path, io=io or ConsoleIO())
    return runner.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m nanobot.cli.wizard")
    parser.add_argument(
        "--config",
        help="Path to the nanoBot config file. Defaults to ~/.nanobot/config.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    io = ConsoleIO()

    try:
        run_wizard(resolve_config_path(args.config), io=io)
    except ConfigLoadError as exc:
        io.write(str(exc))
        return 2
    except KeyboardInterrupt:
        io.write("")
        io.write("已中断。没有写入任何修改。" if getattr(io, "language", "zh") == "zh" else "Interrupted. No changes written.")
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
