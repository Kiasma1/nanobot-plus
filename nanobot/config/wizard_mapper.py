from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Sequence

from nanobot.config.wizard_schema import FieldBinding


ConfigPath = Sequence[str]


def deep_copy_config(config: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(config)


def get_path(config: dict[str, Any], path: ConfigPath, default: Any = None) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def has_path(config: dict[str, Any], path: ConfigPath) -> bool:
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def ensure_path(config: dict[str, Any], path: ConfigPath) -> dict[str, Any]:
    current = config
    for part in path:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    return current


def set_path(config: dict[str, Any], path: ConfigPath, value: Any) -> None:
    if not path:
        raise ValueError("path must not be empty")

    parent = ensure_path(config, path[:-1])
    parent[path[-1]] = value


def read_binding(config: dict[str, Any], binding: FieldBinding, default: Any = None) -> Any:
    for path in binding.all_paths:
        if has_path(config, path):
            return get_path(config, path)

    if default is not None:
        return default
    return binding.default


def set_binding(
    config: dict[str, Any],
    binding: FieldBinding,
    value: Any,
    *,
    clear_fallbacks: bool = False,
) -> None:
    set_path(config, binding.canonical_path, value)
    if clear_fallbacks:
        for path in binding.fallback_paths:
            delete_path(config, path)
            drop_empty_parent_paths(config, path)


def delete_binding(config: dict[str, Any], binding: FieldBinding, *, clear_fallbacks: bool = True) -> None:
    delete_path(config, binding.canonical_path)
    if clear_fallbacks:
        for path in binding.fallback_paths:
            delete_path(config, path)
            drop_empty_parent_paths(config, path)


def canonicalize_binding(config: dict[str, Any], binding: FieldBinding) -> bool:
    if not binding.fallback_paths:
        return False

    fallback_present = any(has_path(config, path) for path in binding.fallback_paths)
    if not fallback_present:
        return False

    set_path(config, binding.canonical_path, read_binding(config, binding))
    for path in binding.fallback_paths:
        delete_path(config, path)
        drop_empty_parent_paths(config, path)
    return True


def canonicalize_bindings(config: dict[str, Any], bindings: Iterable[FieldBinding]) -> list[str]:
    changed: list[str] = []
    for binding in bindings:
        if canonicalize_binding(config, binding):
            changed.append(binding.label)
    return changed


def describe_binding_rewrites(config: dict[str, Any], bindings: Iterable[FieldBinding]) -> list[str]:
    rewrites: list[str] = []
    for binding in bindings:
        for path in binding.fallback_paths:
            if has_path(config, path):
                rewrites.append(f"{'.'.join(path)} -> {binding.label}")
    return rewrites


def strip_root_memory(config: dict[str, Any]) -> bool:
    if not has_path(config, ("memory",)):
        return False
    delete_path(config, ("memory",))
    return True


def delete_path(config: dict[str, Any], path: ConfigPath) -> None:
    if not path:
        raise ValueError("path must not be empty")

    current: Any = config
    for part in path[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]

    if isinstance(current, dict):
        current.pop(path[-1], None)


def mask_secret(value: Any, visible: int = 4) -> str:
    if value in (None, ""):
        return "(not set)"

    text = str(value)
    if len(text) <= visible:
        return "*" * len(text)
    return f"{'*' * max(6, len(text) - visible)}{text[-visible:]}"


def compact_json_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def is_truthy_path(config: dict[str, Any], path: ConfigPath) -> bool:
    return bool(get_path(config, path))


def drop_empty_dicts(config: dict[str, Any], paths: Iterable[ConfigPath]) -> None:
    for path in paths:
        _drop_empty_dict(config, list(path))


def drop_empty_parent_paths(config: dict[str, Any], path: ConfigPath) -> None:
    for depth in range(len(path) - 1, 0, -1):
        parent_path = path[:depth]
        value = get_path(config, parent_path)
        if isinstance(value, dict) and not value:
            delete_path(config, parent_path)


def _drop_empty_dict(config: dict[str, Any], path: list[str]) -> bool:
    if not path:
        return False

    key = path[0]
    value = config.get(key)
    if not isinstance(value, dict):
        return False

    if len(path) == 1:
        if not value:
            config.pop(key, None)
            return True
        return False

    removed = _drop_empty_dict(value, path[1:])
    if removed and not value:
        config.pop(key, None)
        return True
    return removed
