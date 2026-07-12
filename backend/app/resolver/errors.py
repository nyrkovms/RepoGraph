class ResolveConflictError(Exception):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class ResolveNotFoundError(Exception):
    def __init__(self, package: str) -> None:
        super().__init__(f"Package '{package}' not found on PyPI")
        self.package = package


class ResolveUpstreamTimeoutError(Exception):
    def __init__(self, package: str) -> None:
        super().__init__(f"Lazy fetch timeout for package '{package}'")
        self.package = package


class ResolveInternalError(Exception):
    pass
