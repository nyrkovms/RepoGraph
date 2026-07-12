from packaging.utils import canonicalize_name


def normalize_package_name(name: str) -> str:
    """PyPI (PEP 503) normalization. Kept as-is for existing pypi call sites."""
    return canonicalize_name(name)


def normalize_name(ecosystem: str, name: str) -> str:
    """Ecosystem-aware name normalization.

    npm: lowercase only — dots/underscores are significant (`lodash.merge`
    and `lodash-merge` are different packages), scope is part of the name.
    pypi: PEP 503 canonical form.
    """
    if ecosystem == "npm":
        return name.strip().lower()
    return str(canonicalize_name(name))


def npm_scope(name: str) -> str | None:
    """Scope of an npm name: '@types/node' -> 'types', plain names -> None."""
    if name.startswith("@") and "/" in name:
        return name[1:].split("/", 1)[0]
    return None
