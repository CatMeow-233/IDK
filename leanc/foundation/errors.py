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
