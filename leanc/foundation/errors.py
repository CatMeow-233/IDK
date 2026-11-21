from __future__ import annotations

import yaml


class LeanCError(Exception):
    pass


class WorkspaceError(LeanCError):
    pass


class SpecError(LeanCError):
    pass


class ApprovalError(LeanCError):
    pass


class ProviderError(LeanCError):
    pass


class PatchError(LeanCError):
    pass


class ConfigError(LeanCError):
    pass


YAML_LOAD_ERRORS: tuple[type[BaseException], ...] = (
    yaml.YAMLError,
    ValueError,
    KeyError,
    AttributeError,
    RecursionError,
)
JSON_LOAD_ERRORS: tuple[type[BaseException], ...] = (ValueError, RecursionError)
