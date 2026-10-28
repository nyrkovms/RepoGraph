import uuid

from packaging.version import Version

from app.models.dependency import Dependency
from app.models.enums import DependencyType, Ecosystem
from app.ecosystems.pypi.pep508 import parse_pep508
from app.resolver.models import ResolverCandidate
from app.resolver.provider import PyPIProvider

ENV = {"python_version": "3.12", "sys_platform": "linux"}


def _dependency(spec: str) -> Dependency:
    parsed = parse_pep508(spec)
    return Dependency(
        dependency_name=parsed.dependency_name,
        dependency_ecosystem=Ecosystem.pypi,
        version_constraint=parsed.version_constraint,
        dependency_type=DependencyType(parsed.dependency_type),
        extra_name=parsed.extra_name,
        environment_markers=parsed.environment_markers,
        raw_spec=parsed.raw_spec,
    )


class _Session:
    def __init__(self, rows: list[Dependency]) -> None:
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, statement):
        return self

    def scalars(self):
        return self

    def all(self) -> list[Dependency]:
        return self.rows


def _provider(rows: list[Dependency], monkeypatch) -> PyPIProvider:
    provider = PyPIProvider(session_factory=lambda: _Session(rows), environment=ENV, lazy_fetch=lambda name: "fetched")
    monkeypatch.setattr(provider, "_ensure_candidate_dependencies", lambda candidate: None)
    return provider


def _candidate(extras: set[str]) -> ResolverCandidate:
    return ResolverCandidate(
        name="importlib-metadata",
        version=Version("8.7.0"),
        version_id=uuid.uuid4(),
        requires_python=">=3.9",
        extras=frozenset(extras),
    )


ROWS = [
    "zipp>=3.20",
    "requests[socks]>=2",
    'pytest>=6; extra == "test"',
    'sphinx; python_version < "3.10" and extra == "doc"',
    'furo; extra == "doc" or extra == "all"',
]


def test_optional_dependencies_skipped_without_requested_extra(monkeypatch) -> None:
    provider = _provider([_dependency(spec) for spec in ROWS], monkeypatch)

    requirements = provider.get_dependencies(_candidate(set()))

    assert [req.name for req in requirements] == ["zipp", "requests"]
    assert requirements[1].extras == frozenset({"socks"})


def test_optional_dependencies_follow_requested_extras_and_markers(monkeypatch) -> None:
    provider = _provider([_dependency(spec) for spec in ROWS], monkeypatch)

    test_reqs = provider.get_dependencies(_candidate({"test"}))
    # sphinx is limited to python < 3.10; furo is listed under both "doc" and "all".
    doc_reqs = provider.get_dependencies(_candidate({"doc"}))
    all_reqs = provider.get_dependencies(_candidate({"all"}))

    assert [req.name for req in test_reqs] == ["zipp", "requests", "pytest"]
    assert [req.name for req in doc_reqs] == ["zipp", "requests", "furo"]
    assert [req.name for req in all_reqs] == ["zipp", "requests", "furo"]
