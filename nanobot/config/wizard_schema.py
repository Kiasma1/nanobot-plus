from __future__ import annotations

from dataclasses import dataclass


DEFAULT_AGENT_MODEL = "anthropic/claude-opus-4-5"
DEFAULT_GATEWAY_PORT = 18790
DEFAULT_SEARCH_MAX_RESULTS = 5
DEFAULT_CHANNEL_ALLOW_FROM: list[str] = []
DEFAULT_CHANNEL_GROUP_POLICY = "mention"


@dataclass(frozen=True)
class FieldBinding:
    label: str
    canonical_path: tuple[str, ...]
    fallback_paths: tuple[tuple[str, ...], ...] = ()
    default: object | None = None

    @property
    def all_paths(self) -> tuple[tuple[str, ...], ...]:
        return (self.canonical_path,) + self.fallback_paths


CHANNEL_ORDER = ("telegram", "discord", "feishu")
SENSITIVE_KEYS = {
    "apiKey",
    "appSecret",
    "encryptKey",
    "token",
    "verificationToken",
}


@dataclass(frozen=True)
class SecurityPreset:
    slug: str
    description: str
    restrict_to_workspace: bool
    allow_from: list[str]
    group_policy: str


SECURITY_PRESETS = {
    "strict": SecurityPreset(
        slug="strict",
        description="Workspace locked down, explicit allow list required, groups stay mention-only.",
        restrict_to_workspace=True,
        allow_from=[],
        group_policy="mention",
    ),
    "balanced": SecurityPreset(
        slug="balanced",
        description="Safe default for mixed usage: workspace restriction on, groups stay mention-only.",
        restrict_to_workspace=True,
        allow_from=[],
        group_policy="mention",
    ),
    "open-dev": SecurityPreset(
        slug="open-dev",
        description="For local debugging only. Broad access and open group policy.",
        restrict_to_workspace=False,
        allow_from=["*"],
        group_policy="open",
    ),
}


CHANNEL_REQUIRED_FIELDS = {
    "telegram": ("token",),
    "discord": ("token",),
    "feishu": ("appId", "appSecret"),
}


CHANNEL_ACCESS_FIELDS = {
    "telegram": ("allowFrom", "groupPolicy"),
    "discord": ("allowFrom", "groupPolicy"),
    "feishu": ("allowFrom",),
}


RECOMMENDED_SKILLS = [
    {
        "slug": "workspace-guard",
        "title": "Workspace Guard",
        "description": "Helps tighten workspace-scoped defaults and file-operation guardrails, which is useful when you want safer local execution with clearer boundaries.",
    },
    {
        "slug": "telegram-ops",
        "title": "Telegram Ops",
        "description": "Provides maintenance-oriented guidance for Telegram bot health checks, token rotation, and day-to-day operational hygiene.",
    },
    {
        "slug": "search-defaults",
        "title": "Search Defaults",
        "description": "Adds safer Brave Search defaults and guardrails, which is useful when this instance needs web lookup without opening the door to overly loose search behavior.",
    },
] 


MODEL_FIELD = FieldBinding(
    label="agents.defaults.model",
    canonical_path=("agents", "defaults", "model"),
    fallback_paths=(("model", "default"),),
    default=DEFAULT_AGENT_MODEL,
)

WORKSPACE_FIELD = FieldBinding(
    label="agents.defaults.workspace",
    canonical_path=("agents", "defaults", "workspace"),
    fallback_paths=(("paths", "workspace"),),
)

SEND_PROGRESS_FIELD = FieldBinding(
    label="channels.sendProgress",
    canonical_path=("channels", "sendProgress"),
    fallback_paths=(("runtime", "progressStreaming"), ("progressStreaming",)),
    default=True,
)

SEND_TOOL_HINTS_FIELD = FieldBinding(
    label="channels.sendToolHints",
    canonical_path=("channels", "sendToolHints"),
    default=False,
)

GATEWAY_PORT_FIELD = FieldBinding(
    label="gateway.port",
    canonical_path=("gateway", "port"),
    default=DEFAULT_GATEWAY_PORT,
)

SEARCH_API_KEY_FIELD = FieldBinding(
    label="tools.web.search.apiKey",
    canonical_path=("tools", "web", "search", "apiKey"),
    fallback_paths=(("tools", "web", "search", "api_key"),),
    default="",
)

SEARCH_MAX_RESULTS_FIELD = FieldBinding(
    label="tools.web.search.maxResults",
    canonical_path=("tools", "web", "search", "maxResults"),
    fallback_paths=(("tools", "web", "search", "max_results"),),
    default=DEFAULT_SEARCH_MAX_RESULTS,
)

TOOLS_RESTRICT_FIELD = FieldBinding(
    label="tools.restrictToWorkspace",
    canonical_path=("tools", "restrictToWorkspace"),
    fallback_paths=(("restrictToWorkspace",), ("tools", "exec", "restrictToWorkspace")),
    default=False,
)

SUMMARY_FIELDS = (
    MODEL_FIELD,
    WORKSPACE_FIELD,
    GATEWAY_PORT_FIELD,
    SEARCH_API_KEY_FIELD,
    SEND_PROGRESS_FIELD,
    SEND_TOOL_HINTS_FIELD,
    TOOLS_RESTRICT_FIELD,
)


CANONICAL_FIELD_BINDINGS = (
    MODEL_FIELD,
    WORKSPACE_FIELD,
    SEND_PROGRESS_FIELD,
    SEND_TOOL_HINTS_FIELD,
    GATEWAY_PORT_FIELD,
    SEARCH_API_KEY_FIELD,
    SEARCH_MAX_RESULTS_FIELD,
    TOOLS_RESTRICT_FIELD,
)


LEGACY_FALLBACK_BINDINGS = tuple(
    binding for binding in CANONICAL_FIELD_BINDINGS if binding.fallback_paths
)


CHANNEL_DEFAULT_CONFIGS = {
    "telegram": {
        "enabled": False,
        "token": "",
        "allowFrom": [],
        "proxy": None,
        "replyToMessage": False,
        "groupPolicy": "mention",
    },
    "discord": {
        "enabled": False,
        "token": "",
        "allowFrom": [],
        "gatewayUrl": "wss://gateway.discord.gg/?v=10&encoding=json",
        "intents": 37377,
        "groupPolicy": "mention",
    },
    "feishu": {
        "enabled": False,
        "appId": "",
        "appSecret": "",
        "encryptKey": "",
        "verificationToken": "",
        "allowFrom": [],
        "reactEmoji": "THUMBSUP",
    },
}


STATUS_LABELS = {
    "zh": {
        "existing, unchanged": "已存在，未修改",
        "modified": "已修改",
        "added": "本次新增",
        "not configured": "未配置",
    },
    "en": {
        "existing, unchanged": "Existing, unchanged",
        "modified": "Modified",
        "added": "Added",
        "not configured": "Not configured",
    },
}


def keep_modify_skip_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "保持当前值"),
            ("2", "修改"),
            ("3", "跳过本项"),
        )
    return (
        ("1", "Keep current"),
        ("2", "Modify"),
        ("3", "Skip this item"),
    )


def group_policy_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "mention"),
            ("2", "open"),
            ("3", "自定义文本"),
        )
    return (
        ("1", "mention"),
        ("2", "open"),
        ("3", "custom text"),
    )


def skills_step_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "跳过"),
            ("2", "打印推荐的安装命令"),
            ("3", "只显示推荐列表"),
        )
    return (
        ("1", "Skip"),
        ("2", "Print recommended install commands"),
        ("3", "Show the recommendation list only"),
    )


def skills_follow_up_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "继续查看配置摘要"),
            ("2", "返回推荐菜单"),
            ("3", "退出，不应用任何修改"),
        )
    return (
        ("1", "Continue to configuration summary"),
        ("2", "Go back to the recommendations menu"),
        ("3", "Exit without applying changes"),
    )


def security_preset_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "保持当前值"),
            ("2", "严格模式"),
            ("3", "平衡模式"),
            ("4", "开发开放模式"),
            ("5", "不使用预设，手动逐项检查"),
        )
    return (
        ("1", "Keep current values"),
        ("2", "strict"),
        ("3", "balanced"),
        ("4", "open-dev"),
        ("5", "Skip preset and review fields manually"),
    )


def telegram_maintenance_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "保持 Telegram 当前状态不变"),
            ("2", "更新 Telegram token"),
            ("3", "暂时禁用 Telegram"),
            ("4", "保持 Telegram 不变，并新增其他渠道"),
            ("5", "复制为独立 Telegram 实例"),
            ("6", "检查当前 Telegram 实例的安全配置"),
            ("7", "修改 Telegram 访问控制"),
        )
    return (
        ("1", "Keep Telegram unchanged"),
        ("2", "Update Telegram token"),
        ("3", "Temporarily disable Telegram"),
        ("4", "Keep Telegram and review Discord / Feishu"),
        ("5", "Copy to a standalone Telegram instance"),
        ("6", "Check Telegram safety recommendations"),
        ("7", "Modify Telegram access"),
    )


def telegram_new_options(language: str) -> tuple[tuple[str, str], ...]:
    if language == "zh":
        return (
            ("1", "暂不配置 Telegram"),
            ("2", "启用 Telegram"),
        )
    return (
        ("1", "Leave Telegram unconfigured"),
        ("2", "Enable Telegram"),
    )
