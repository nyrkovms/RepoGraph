"""PEP 508 environment for the dependency tree and resolve (SPEC-MULTI-ECOSYSTEM.md §7.3).

Same semantics as the tree endpoint today (api/v1/packages.py): without an environment every marker applies;
with optional dependencies included, `extra == ...` conditions are ignored and the rest must hold.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from packaging.markers import InvalidMarker, Marker, UndefinedComparison, UndefinedEnvironmentName

from app.ecosystems.base import NormalizedDependency

_OS_DEFAULTS = {
    "linux": ("linux", "posix", "Linux"),
    "windows": ("win32", "nt", "Windows"),
    "win32": ("win32", "nt", "Windows"),
    "darwin": ("darwin", "posix", "Darwin"),
    "mac": ("darwin", "posix", "Darwin"),
    "macos": ("darwin", "posix", "Darwin"),
}


@dataclass(frozen=True, slots=True)
class PythonEnvironment:
    python_version: str = "3.12"
    python_full_version: str = "3.12.0"
    sys_platform: str = "linux"
    os_name: str = "posix"
    platform_machine: str = "x86_64"
    platform_system: str = "Linux"
    implementation_name: str = "cpython"
    platform_python_implementation: str = "CPython"
    # False when the caller gave no environment: markers are then not evaluated (tree endpoint behaviour).
    specified: bool = True

    def as_dict(self) -> Mapping[str, object]:
        return asdict(self)

    def marker_env(self) -> dict[str, str]:
        values = asdict(self)
        values.pop("specified")
        return values


def parse_environment(raw: Mapping[str, object]) -> PythonEnvironment:
    """Accepts the API's short form (`python_version`, `os`) and full PEP 508 names."""
    if not raw:
        return PythonEnvironment(specified=False)
    values = {key: str(value) for key, value in raw.items() if value is not None and key != "specified"}
    short_os = values.pop("os", None)
    if short_os is not None:
        platform, os_name, system = _OS_DEFAULTS.get(short_os.lower(), ("", "", ""))
        values.setdefault("sys_platform", platform)
        values.setdefault("os_name", os_name)
        values.setdefault("platform_system", system)
    if "python_version" in values and "python_full_version" not in values:
        values["python_full_version"] = f"{values['python_version']}.0"
    known = {name: values[name] for name in PythonEnvironment.__dataclass_fields__ if name in values}
    return PythonEnvironment(**known)  # type: ignore[arg-type]


def _evaluate(expression: str, env: PythonEnvironment, extra: str | None = None) -> bool:
    values = env.marker_env()
    if extra is not None:
        values["extra"] = extra
    try:
        return Marker(expression).evaluate(values)
    except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName):
        return True


def dependency_applies(dep: NormalizedDependency, env: PythonEnvironment, include_optional: bool) -> bool:
    if dep.dependency_type == "optional" and not include_optional:
        return False
    if dep.dependency_type not in ("runtime", "optional"):
        return False
    markers = dep.environment_markers
    if not env.specified or not isinstance(markers, Mapping):
        return True
    expression = markers.get("expression")
    if not isinstance(expression, str) or not expression:
        return True
    if not include_optional:
        return _evaluate(expression, env)
    conditions = markers.get("conditions")
    if not isinstance(conditions, list):
        return _evaluate(expression, env)
    rest = [c for c in conditions if isinstance(c, Mapping) and c.get("marker") != "extra"]
    return all(_evaluate(f"{c.get('marker')} {c.get('op')} {str(c.get('value'))!r}", env) for c in rest)
