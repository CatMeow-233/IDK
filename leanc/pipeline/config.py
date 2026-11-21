"""Optional project configuration (``leanc.yaml``), which can never weaken verification or isolation."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leanc.foundation.errors import ConfigError

CONFIG_FILE = "leanc.yaml"
IGNORED_ALLOW_UNSANDBOXED = (
    "accepted for backward compatibility but ignored: the project file is not covered by the spec approval, "
    "so only the --allow-unsandboxed CLI flag can disable isolation"
)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "mock"
    mock_dir: str = "mock"
    sandbox: Literal["auto", "none"] = "auto"
    allow_unsandboxed: bool = Field(default=False, description=IGNORED_ALLOW_UNSANDBOXED)


def load_config(root: Path) -> ProjectConfig:
    path = Path(root) / CONFIG_FILE
    if not path.is_file():
        return ProjectConfig()
    if path.is_symlink():
        raise ConfigError(f"{path}: must not be a symbolic link")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: cannot parse: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8 (byte offset {exc.start})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid configuration: {exc}") from None
