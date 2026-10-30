"""NuGet package ids and purls: case-insensitive, stored lowercase (SPEC-MULTI-ECOSYSTEM.md §4).

`validate_id` follows NuGet's `PackageIdValidator`. The identity of a package is the lowercased id; the spelling the
author chose comes from the registry and is kept by the adapter as the display name, not in `PackageRef`.
"""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

MAX_ID_LENGTH = 100

# PackageIdValidator: `^\w+([_.-]\w+)*$`. `\w` includes `_` and, as in .NET, Unicode letters and digits, so a leading
# `_` is fine while a leading, trailing or doubled `.`/`-` is not (`_` doubles freely, being a word character).
_ID = re.compile(r"\w+(?:[_.-]\w+)*")


def validate_id(raw: str) -> None:
    if len(raw) > MAX_ID_LENGTH:
        raise ValueError(f"NuGet id is longer than {MAX_ID_LENGTH} characters: {raw!r}")
    if not _ID.fullmatch(raw):
        raise ValueError(
            f"not a NuGet id (letters, digits, '_', and '.' or '-' only between word characters): {raw!r}"
        )


def registration_path(ref: PackageRef) -> str:
    """The id as RegistrationsBaseUrl and PackageBaseAddress expect it in a URL: lowercase."""
    return ref.name


class NuGetCoordinates:
    def parse(self, raw: str) -> PackageRef:
        package_id = raw.strip()
        validate_id(package_id)
        return PackageRef(ecosystem="nuget", namespace="", name=package_id.lower())

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        parsed = parse(purl)
        if parsed.type != "nuget":
            raise ValueError(f"not a nuget purl: {purl}")
        if parsed.namespace:
            raise ValueError(f"nuget purls have no namespace: {purl}")
        return self.parse(parsed.name), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        """The purl spec says NuGet names are case-insensitive, so `pkg:nuget/Newtonsoft.Json` and
        `pkg:nuget/newtonsoft.json` name one package; we write the id as stored, lowercase."""
        return Purl(type="nuget", namespace=None, name=ref.name, version=version).to_string()

    def display(self, ref: PackageRef) -> str:
        return ref.name
