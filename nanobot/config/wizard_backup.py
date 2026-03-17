from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil


def build_backup_path(config_path: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return config_path.with_name(f"{config_path.name}.bak.{timestamp}")


def backup_config(config_path: Path, now: datetime | None = None) -> Path | None:
    if not config_path.exists():
        return None

    backup_path = build_backup_path(config_path, now=now)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, backup_path)
    return backup_path
