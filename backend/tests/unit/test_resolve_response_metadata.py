from uuid import uuid4

from packaging.version import Version

from app.resolver.models import ResolverCandidate, ResolverRequirement
from app.resolver.service import _is_newer_version, _select_visible_roots, _serialize_dependencies
from app.resolver.provider import PyPIProvider, _artifacts_allow_environment
from app.schemas.resolve import ResolvedDependencyNode, ResolvedPackage


class _Provider:
    def __init__(self, dependencies_map):
        self.dependencies_map = dependencies_map

    def get_dependencies(self, candidate):
        return self.dependencies_map.get(candidate.name, [])


def test_serialize_dependencies_preserves_marker_metadata() -> None:
    root = ResolverCandidate(
        name="flask",
        version=Version("3.1.3"),
        version_id=uuid4(),
        requires_python=">=3.9",
        extras=frozenset({"speedups"}),
    )
    dep = ResolverCandidate(
        name="werkzeug",
        version=Version("3.1.0"),
        version_id=uuid4(),
        requires_python=">=3.9",
        extras=frozenset(),
    )

    req = ResolverRequirement.from_input(
        name="werkzeug",
        version_spec=">=3.1.0",
        dependency_type="runtime",
        extra_name=None,
        environment_markers={
            "expression": 'python_version < "3.13" and extra == "speedups"',
            "conditions": [
                {"marker": "python_version", "op": "<", "value": "3.13"},
                {"marker": "extra", "op": "==", "value": "speedups"},
            ],
        },
    )

    nodes = _serialize_dependencies(
        provider=_Provider({"flask": [req], "werkzeug": []}),
        mapping={"flask": root, "werkzeug": dep},
        candidate=root,
        max_depth=5,
        depth=1,
        seen={"flask"},
    )

    assert len(nodes) == 1
    assert nodes[0].name == "werkzeug"
    assert nodes[0].constraint == ">=3.1.0"
    assert nodes[0].version == "3.1.0"
    assert nodes[0].dependency_type == "runtime"
    assert nodes[0].extra_name is None
    assert nodes[0].environment_markers["conditions"][1]["value"] == "speedups"


def test_select_visible_roots_hides_requested_packages_already_covered_transitively() -> None:
    requirements = [
        ResolverRequirement.from_input("pydantic", "==2.12.5"),
        ResolverRequirement.from_input("annotated-types", "==0.7.0"),
        ResolverRequirement.from_input("pydantic-core", "==2.41.5"),
    ]
    resolved_by_root = {
        "pydantic": ResolvedPackage(
            version="2.12.5",
            dependencies=[
                ResolvedDependencyNode(
                    name="annotated-types",
                    version="0.7.0",
                    constraint=">=0.6.0",
                    dependency_type="runtime",
                    extra_name=None,
                    environment_markers=None,
                    dependencies=[],
                ),
                ResolvedDependencyNode(
                    name="pydantic-core",
                    version="2.41.5",
                    constraint="==2.41.5",
                    dependency_type="runtime",
                    extra_name=None,
                    environment_markers=None,
                    dependencies=[],
                ),
            ],
        ),
        "annotated-types": ResolvedPackage(version="0.7.0", dependencies=[]),
        "pydantic-core": ResolvedPackage(version="2.41.5", dependencies=[]),
    }

    visible_roots, hidden_roots = _select_visible_roots(requirements, resolved_by_root)

    assert visible_roots == ["pydantic"]
    assert hidden_roots == ["annotated-types", "pydantic-core"]


def test_is_newer_version_detects_upgrade() -> None:
    assert _is_newer_version("3.1.3", "3.0.0") is True
    assert _is_newer_version("3.0.0", "3.0.0") is False
    assert _is_newer_version(None, "3.0.0") is False


def test_artifacts_allow_environment_uses_sdist_or_matching_wheels() -> None:
    windows_env = {"sys_platform": "win32"}
    linux_env = {"sys_platform": "linux"}

    assert (
        _artifacts_allow_environment(
            {"files": [{"packagetype": "sdist", "filename": "demo-1.0.0.tar.gz"}]},
            linux_env,
        )
        is True
    )
    assert (
        _artifacts_allow_environment(
            {
                "files": [
                    {"packagetype": "bdist_wheel", "filename": "demo-1.0.0-cp312-cp312-win_amd64.whl"},
                ]
            },
            windows_env,
        )
        is True
    )
    assert (
        _artifacts_allow_environment(
            {
                "files": [
                    {"packagetype": "bdist_wheel", "filename": "demo-1.0.0-cp312-cp312-win_amd64.whl"},
                ]
            },
            linux_env,
        )
        is False
    )


def test_get_latest_compatible_version_prefers_stable_over_prerelease() -> None:
    provider = object.__new__(PyPIProvider)
    provider._load_candidates = lambda _identifier: [  # type: ignore[attr-defined]
        ResolverCandidate(
            name="pydantic",
            version=Version("2.13.0b2"),
            version_id=uuid4(),
            requires_python=">=3.9",
            extras=frozenset(),
        ),
        ResolverCandidate(
            name="pydantic",
            version=Version("2.12.6"),
            version_id=uuid4(),
            requires_python=">=3.9",
            extras=frozenset(),
        ),
    ]

    assert provider.get_latest_compatible_version("pydantic") == "2.12.6"
