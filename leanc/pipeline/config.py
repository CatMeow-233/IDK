from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from leanc.foundation.errors import YAML_LOAD_ERRORS, ConfigError

CONFIG_FILE = "leanc.yaml"


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "mock"
    mock_dir: str = "mock"
    sandbox: Literal["auto", "none"] = "auto"
    allow_unsandboxed: bool = False


def load_config(root: Path) -> ProjectConfig:
    path = Path(root) / CONFIG_FILE
    if not path.is_file():
        return ProjectConfig()
    if path.is_symlink():
        raise ConfigError(f"{path}: must not be a symbolic link")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except YAML_LOAD_ERRORS as exc:
        raise ConfigError(f"{path}: cannot parse: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8 (byte offset {exc.start})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid configuration: {exc}") from None
