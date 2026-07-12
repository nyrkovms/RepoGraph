from packaging.utils import canonicalize_name


def normalize_package_name(name: str) -> str:
    return canonicalize_name(name)
