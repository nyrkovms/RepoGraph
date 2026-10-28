"""PyPI names and purls: PEP 503 normalization, no namespace (SPEC-MULTI-ECOSYSTEM.md §4)."""

from packaging.utils import canonicalize_name

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse


class PyPICoordinates:
    def parse(self, raw: str) -> PackageRef:
        name = raw.strip()
        if not name:
            raise ValueError("empty PyPI project name")
        return PackageRef(ecosystem="pypi", namespace="", name=canonicalize_name(name))

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        parsed = parse(purl)
        if parsed.type != "pypi":
            raise ValueError(f"not a PyPI purl: {purl}")
        if parsed.namespace:
            raise ValueError(f"PyPI purls have no namespace: {purl}")
        return self.parse(parsed.name), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        return Purl(type="pypi", name=ref.name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return ref.name
