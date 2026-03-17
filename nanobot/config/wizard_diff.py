from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nanobot.config.wizard_mapper import compact_json_value, get_path, mask_secret, read_binding
from nanobot.config.wizard_schema import CHANNEL_ORDER, CHANNEL_REQUIRED_FIELDS, SENSITIVE_KEYS, STATUS_LABELS, SUMMARY_FIELDS


@dataclass(frozen=True)
class DiffEntry:
    path: tuple[str, ...]
    kind: str
    old: Any
    new: Any


@dataclass(frozen=True)
class ChannelSummary:
    name: str
    status: str
    detail: str


def diff_configs(old: Any, new: Any, prefix: tuple[str, ...] = ()) -> list[DiffEntry]:
    if isinstance(old, dict) and isinstance(new, dict):
        entries: list[DiffEntry] = []
        keys = sorted(set(old) | set(new))
        for key in keys:
            key_prefix = prefix + (key,)
            if key not in old:
                entries.append(DiffEntry(path=key_prefix, kind="added", old=None, new=new[key]))
            elif key not in new:
                entries.append(DiffEntry(path=key_prefix, kind="removed", old=old[key], new=None))
            else:
                entries.extend(diff_configs(old[key], new[key], key_prefix))
        return entries

    if old != new:
        return [DiffEntry(path=prefix, kind="changed", old=old, new=new)]
    return []


def summarize_channels(old_config: dict[str, Any], new_config: dict[str, Any]) -> list[ChannelSummary]:
    summaries: list[ChannelSummary] = []
    for channel_name in CHANNEL_ORDER:
        old_channel = get_path(old_config, ("channels", channel_name), {}) or {}
        new_channel = get_path(new_config, ("channels", channel_name), {}) or {}

        old_configured = channel_configured(channel_name, old_channel)
        new_configured = channel_configured(channel_name, new_channel)

        if not old_configured and not new_configured:
            status = "not configured"
            detail = "No configuration detected."
        elif old_configured and not new_configured:
            status = "modified"
            detail = "Was configured before, now disabled or incomplete."
        elif not old_configured and new_configured:
            status = "added"
            detail = "New channel will be configured in this run."
        elif old_channel == new_channel:
            status = "existing, unchanged"
            detail = "Existing channel config will be kept as-is."
        else:
            status = "modified"
            detail = "Existing channel config will be updated."

        summaries.append(ChannelSummary(name=channel_name, status=status, detail=detail))
    return summaries


def channel_configured(channel_name: str, channel_config: dict[str, Any]) -> bool:
    if not isinstance(channel_config, dict):
        return False

    required_fields = CHANNEL_REQUIRED_FIELDS.get(channel_name, ())
    enabled = bool(channel_config.get("enabled"))
    has_required = all(bool(channel_config.get(field)) for field in required_fields)
    return enabled and has_required


def render_summary(
    original_config: dict[str, Any],
    draft_config: dict[str, Any],
    config_path: Path,
    *,
    language: str = "en",
    notes: list[str] | None = None,
    split_summary: str | None = None,
) -> str:
    diff_entries = diff_configs(original_config, draft_config)
    is_zh = language == "zh"
    lines = [
        "配置摘要" if is_zh else "Configuration summary",
        f"{'目标配置文件' if is_zh else 'Target config file'}{'：' if is_zh else ':'}",
        str(config_path),
    ]

    if notes:
        lines.append("")
        lines.append("旧格式处理说明：" if is_zh else "Compatibility notes:")
        for note in notes:
            lines.append(f"- {note}")

    lines.append("")
    lines.append("字段变更：" if is_zh else "Field changes:")
    if diff_entries:
        for binding in SUMMARY_FIELDS:
            old_value = read_binding(original_config, binding)
            new_value = read_binding(draft_config, binding)
            if old_value == new_value:
                if is_zh:
                    lines.append(f"- {binding.label}: 保持不变（{_format_value(binding.canonical_path, new_value, language)}）")
                else:
                    lines.append(f"- {binding.label}: unchanged ({_format_value(binding.canonical_path, new_value, language)})")
            elif old_value is None:
                if is_zh:
                    lines.append(f"- {binding.label}: 新增（{_format_value(binding.canonical_path, new_value, language)}）")
                else:
                    lines.append(
                        f"- {binding.label}: added ({_format_value(binding.canonical_path, new_value, language)})"
                    )
            else:
                lines.append(
                    f"- {binding.label}: {_format_value(binding.canonical_path, old_value, language)} -> {_format_value(binding.canonical_path, new_value, language)}"
                )
    else:
        lines.append(
            "- 当前实例没有计划写入的字段变更。"
            if is_zh
            else "- No changes planned for the current instance."
        )

    lines.append("")
    lines.append("渠道状态：" if is_zh else "Channel status:")
    for summary in summarize_channels(original_config, draft_config):
        lines.append(
            f"- {summary.name.capitalize()}: {STATUS_LABELS[language].get(summary.status, summary.status)}"
        )

    if diff_entries:
        lines.append("")
        lines.append("详细变更：" if is_zh else "Detailed diff:")
        for entry in diff_entries:
            dotted = ".".join(entry.path)
            lines.append(
                f"- {dotted}: {_format_value(entry.path, entry.old, language)} -> {_format_value(entry.path, entry.new, language)}"
            )

    if split_summary:
        lines.append("")
        lines.append(split_summary)

    return "\n".join(lines)


def _format_value(path: tuple[str, ...], value: Any, language: str) -> str:
    if path and path[-1] in SENSITIVE_KEYS:
        return mask_secret(value)
    if value is None:
        return "未设置" if language == "zh" else "(unset)"
    return compact_json_value(value)
