"""RPM coordinates: `<distro>-<release>/<binary package name>` (SPEC-MULTI-ECOSYSTEM.md §4, §6.4).

The namespace is part of the identity because `openssl` in RHEL 9 and in Rocky 9 are different packages with
different build histories. Architecture and module stream are properties of an artifact and a version, not of the
package, so they stay out of `PackageRef`; the purl reader hands `arch` back separately.

RPM names are case-sensitive and are kept as given.
"""

import re

from app.ecosystems.base import PackageRef
from app.ecosystems.purl import Purl, parse

# Package names: what rpm itself accepts in a Name tag, minus anything that could not sit in a file name or purl.
_NAME = re.compile(r"[A-Za-z0-9._+][A-Za-z0-9._+-]*")
# `rhel-9`, `rocky-9`, `fedora-41`, `rhel-9.4`, `epel-next-9`: lowercase words joined by '-', ending in the release.
_NAMESPACE = re.compile(r"(?P<distro>[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*)-(?P<release>[0-9]+(?:\.[0-9]+)*)")
_DIGITS = re.compile(r"[0-9]+")

# Distribution part of the namespace -> purl vendor. EPEL packages are built by Fedora.
_VENDORS = {"rhel": "redhat", "alma": "almalinux", "epel": "fedora"}

# Without a known set, `openssl-3.0.7-27.el9` would parse with arch `el9`: the release has dots of its own.
_ARCHES = frozenset({
    "noarch", "src", "nosrc", "x86_64", "i386", "i486", "i586", "i686", "aarch64", "ppc", "ppc64", "ppc64le",
    "s390", "s390x", "armv7hl", "armhfp", "riscv64",
})


def validate_name(raw: str) -> None:
    if not raw:
        raise ValueError("RPM package name is empty")
    if not _NAME.fullmatch(raw):
        raise ValueError(
            f"not an RPM package name (letters, digits and '._+-' only, not starting with '-'): {raw!r}"
        )


def validate_namespace(raw: str) -> None:
    _match_namespace(raw)


def _match_namespace(raw: str) -> re.Match[str]:
    match = _NAMESPACE.fullmatch(raw)
    if match is None:
        raise ValueError(
            f"not an RPM namespace (lowercase '<distribution>-<release>', e.g. 'rocky-9' or 'rhel-9.4'): {raw!r}"
        )
    return match


def distro_of(namespace: str) -> tuple[str, str]:
    """`rocky-9` -> `("rocky", "9")`, `rhel-9.4` -> `("rhel", "9.4")`."""
    match = _match_namespace(namespace)
    return match["distro"], match["release"]


def vendor_of(namespace: str) -> str:
    """The purl vendor of the namespace's distribution: `rhel-9` -> `redhat`, `epel-9` -> `fedora`."""
    distro, _ = distro_of(namespace)
    return _VENDORS.get(distro, distro)


def nevra(name: str, epoch: int | str | None, version: str, release: str, arch: str) -> str:
    """`name-[epoch:]version-release.arch`; a zero or missing epoch is left out, as `rpm -q` does."""
    validate_name(name)
    for label, part in (("version", version), ("release", release)):
        if not part or "-" in part or ":" in part or "/" in part or part.strip() != part:
            raise ValueError(f"not an RPM {label}: {part!r}")
    if not re.fullmatch(r"[A-Za-z0-9_]+", arch):
        raise ValueError(f"not an RPM arch: {arch!r}")
    epoch_text = str(epoch).strip() if epoch is not None else ""
    if epoch_text and not _DIGITS.fullmatch(epoch_text):
        raise ValueError(f"not an RPM epoch: {epoch!r}")
    prefix = f"{int(epoch_text)}:" if epoch_text and int(epoch_text) else ""
    return f"{name}-{prefix}{version}-{release}.{arch}"


def parse_nevra(text: str) -> tuple[str, int, str, str, str]:
    """Inverse of `nevra`: `(name, epoch, version, release, arch)`, epoch 0 when absent.

    Package names contain '-', so the text is split from the right: arch after the last '.', release after the last
    '-' before it, then the version with its optional `epoch:`.
    """
    stem, dot, arch = text.strip().rpartition(".")
    if not dot or arch not in _ARCHES:
        raise ValueError(f"not a NEVRA (no known arch after the last '.'): {text!r}")
    rest, dash, release = stem.rpartition("-")
    if not dash or not release:
        raise ValueError(f"not a NEVRA (no release): {text!r}")
    name, dash, evr = rest.rpartition("-")
    if not dash or not evr:
        raise ValueError(f"not a NEVRA (no version): {text!r}")
    epoch_text, colon, version = evr.rpartition(":")
    if colon and not _DIGITS.fullmatch(epoch_text):
        raise ValueError(f"not a NEVRA (bad epoch): {text!r}")
    epoch = int(epoch_text) if colon else 0
    # Re-serializing validates every part, including a name that is empty or starts with '-'.
    nevra(name, epoch, version, release, arch)
    return name, epoch, version, release, arch


def package_matches(ref: PackageRef, pattern: str) -> bool:
    """Matcher of `routing.packages` (§6.2): an exact name or a glob where `*` is any run of characters
    (`python3-*`). Case-sensitive, like RPM names; `*` alone is every package."""
    text = pattern.strip()
    if not text:
        return False
    return re.fullmatch(".*".join(re.escape(part) for part in text.split("*")), ref.name) is not None


def purl_qualifiers(purl: str) -> dict[str, str]:
    """The `arch` qualifier of an rpm purl, when present."""
    arch = _parse_rpm_purl(purl).qualifiers.get("arch")
    return {"arch": arch} if arch else {}


def _parse_rpm_purl(purl: str) -> Purl:
    parsed = parse(purl)
    if parsed.type != "rpm":
        raise ValueError(f"not an rpm purl: {purl}")
    return parsed


class RpmCoordinates:
    def parse(self, raw: str) -> PackageRef:
        """`<namespace>/<name>`, split on the first '/': `rocky-9/openssl`."""
        namespace, slash, name = raw.strip().partition("/")
        if not slash:
            raise ValueError(f"expected '<distro>-<release>/<package>', e.g. 'rocky-9/openssl': {raw!r}")
        return self._ref(namespace, name)

    def from_purl(self, purl: str) -> tuple[PackageRef, str | None]:
        """The namespace comes from the `distro` qualifier. The purl vendor is not checked against it: the same
        distribution is spelt differently in the wild (`redhat`/`rhel`, `almalinux`/`alma`). `arch` and `epoch`
        qualifiers do not change identity; read `arch` with `purl_qualifiers`."""
        parsed = _parse_rpm_purl(purl)
        distro = parsed.qualifiers.get("distro")
        if not distro:
            raise ValueError(f"rpm purls need a 'distro' qualifier (the namespace, e.g. rocky-9): {purl}")
        if not parsed.namespace or "/" in parsed.namespace:
            raise ValueError(f"rpm purls need exactly one vendor segment (pkg:rpm/<vendor>/<name>): {purl}")
        return self._ref(distro, parsed.name), parsed.version

    def to_purl(self, ref: PackageRef, version: str | None = None) -> str:
        """`pkg:rpm/<vendor>/<name>[@<evr>]?distro=<namespace>`."""
        return Purl(
            type="rpm",
            namespace=vendor_of(ref.namespace),
            name=ref.name,
            version=version,
            qualifiers={"distro": ref.namespace},
        ).to_string()

    def display(self, ref: PackageRef) -> str:
        return f"{ref.namespace}/{ref.name}"

    @staticmethod
    def _ref(namespace: str, name: str) -> PackageRef:
        validate_namespace(namespace)
        validate_name(name)
        return PackageRef(ecosystem="rpm", namespace=namespace, name=name)


__all__ = [
    "RpmCoordinates",
    "distro_of",
    "nevra",
    "package_matches",
    "parse_nevra",
    "purl_qualifiers",
    "validate_name",
    "validate_namespace",
    "vendor_of",
]
