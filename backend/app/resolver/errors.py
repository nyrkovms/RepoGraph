class ResolveConflictError(Exception):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ResolveNotFoundError(Exception):
    def __init__(self, package: str) -> None:
        super().__init__(f"Package '{package}' not found in any source")
        self.package = package


class ResolveInternalError(Exception):
    pass


class ResolveInvalidRequest(Exception):
    """Unknown ecosystem or profile, a root that is not a purl of the ecosystem: 422."""


class ResolveLimitError(Exception):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ResolveTimeoutError(Exception):
    pass
