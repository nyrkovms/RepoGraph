"""The environment the tests run in must have the versions pinned in uv.lock (CI image, stand, local uv)."""

import re
import tomllib
from importlib import metadata
from pathlib import Path

LOCK = Path(__file__).resolve().parents[2] / "uv.lock"


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_installed_versions_match_lock() -> None:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    locked = {
        _normalized(package["name"]): package["version"]
        for package in lock["package"]
        if "registry" in package.get("source", {})
    }
    installed = {_normalized(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}
    drift = {
        name: f"installed {installed[name]}, locked {version}"
        for name, version in sorted(locked.items())
        if name in installed and installed[name] != version
    }
    assert not drift, f"installed packages differ from uv.lock: {drift}"
    # The packages the API schema and the OpenAPI snapshot depend on must be installed from the lock at all.
    assert {"fastapi", "pydantic", "starlette"} <= installed.keys() & locked.keys()
