"""Exception hierarchy.  CLI maps these to readable messages and exit codes."""


class LeanCError(Exception):
    """Base class for expected, user-facing errors."""


class WorkspaceError(LeanCError):
    """Path traversal, symlink escape or other workspace-boundary violation."""


class SpecError(LeanCError):
    """Malformed or invalid specification."""


class ApprovalError(LeanCError):
    """Approval store problem (missing key, corrupt record, ...)."""


class ProviderError(LeanCError):
    """LLM provider failure (network, auth, malformed response)."""


class PatchError(LeanCError):
    """A proposed patch could not be parsed or applied."""


class ConfigError(LeanCError):
    """Invalid project configuration."""
