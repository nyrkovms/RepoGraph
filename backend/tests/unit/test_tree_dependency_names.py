"""Tree nodes name a dependency as its ecosystem writes it (the dependency row keeps namespace and name apart)."""

from app.api.v1.package_detail import _dependency_display
from app.ecosystems.registry import load_builtin_plugins
from app.models.dependency import Dependency
from app.models.enums import Ecosystem

load_builtin_plugins()


def _dep(ecosystem: Ecosystem, namespace: str, name: str) -> Dependency:
    return Dependency(dependency_ecosystem=ecosystem, dependency_namespace=namespace, dependency_name=name)


def test_scoped_npm_and_maven_names_keep_their_namespace() -> None:
    assert _dependency_display(_dep(Ecosystem.npm, "types", "node")) == "@types/node"
    assert _dependency_display(_dep(Ecosystem.maven, "com.fasterxml.jackson.core", "jackson-core")) == \
        "com.fasterxml.jackson.core:jackson-core"
    assert _dependency_display(_dep(Ecosystem.pypi, "", "requests")) == "requests"
    assert _dependency_display(_dep(Ecosystem.rpm, "fedora", "curl")) == "curl"     # no plugin yet: the stored name
