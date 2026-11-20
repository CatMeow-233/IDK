"""Optional project configuration (``leanc.yaml`` at the project root).

Example::

    provider: mock          # default LLM provider for generate/repair/run (mock | openai)
    mock_dir: mock          # fixture directory for the mock provider
    sandbox: auto           # auto (bwrap if functional) | none
    allow_unsandboxed: false

The configuration never weakens verification: it cannot change checks, bounds,
gates or approval rules (those live in the approved spec), and it cannot allow
unsandboxed execution of untrusted code (``allow_unsandboxed`` is ignored here;
only the ``--allow-unsandboxed`` CLI flag enables that).  ``sandbox: none``
only makes Lean C *skip* dynamic tests (the result is then UNKNOWN).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from leanc.core.errors import ConfigError

CONFIG_FILE = "leanc.yaml"


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "mock"
    mock_dir: str = "mock"
    sandbox: Literal["auto", "none"] = "auto"
    # Accepted for backward compatibility but IGNORED: a project file is not
    # trusted (it is not covered by the spec approval), so it must not be able
    # to turn off isolation of untrusted code.  Use the CLI flag
    # --allow-unsandboxed for an explicit, per-invocation opt-in.
    allow_unsandboxed: bool = False


def load_config(root: Path) -> ProjectConfig:
    p = Path(root) / CONFIG_FILE
    if not p.is_file():
        return ProjectConfig()
    if p.is_symlink():
        raise ConfigError(f"{p}: must not be a symbolic link")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: cannot parse: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{p}: not valid UTF-8 (byte offset {exc.start})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top level must be a mapping")
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{p}: invalid configuration: {exc}") from None
