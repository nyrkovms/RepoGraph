"""Representative distribution selection for probe (PyPI).

Ported from classifire: probe one wheel per version, prefer the
cross-platform (`none-any`) build, respect the size limit. No suitable
wheel is a permanent probe result, not a retryable error.
"""

from dataclasses import dataclass
from typing import Iterable, Protocol

NO_WHEEL_AVAILABLE = "no wheel available"
ALL_WHEELS_EXCEED_LIMIT = "all wheels exceed size limit"


class ArtifactLike(Protocol):
    filename: str
    download_url: str
    size_bytes: int | None
    artifact_type: str


@dataclass(frozen=True)
class WheelCandidate:
    filename: str
    download_url: str
    size_bytes: int | None

    @property
    def platform_wheel(self) -> bool:
        return "none-any" not in self.filename


@dataclass(frozen=True)
class WheelSelection:
    candidate: WheelCandidate | None
    error: str | None


def select_representative_wheel(artifacts: Iterable[ArtifactLike], max_bytes: int) -> WheelSelection:
    wheels = [a for a in artifacts if a.artifact_type == "wheel" and a.filename.endswith(".whl")]
    if not wheels:
        return WheelSelection(candidate=None, error=NO_WHEEL_AVAILABLE)

    fitting = [a for a in wheels if (a.size_bytes or 0) <= max_bytes]
    if not fitting:
        return WheelSelection(candidate=None, error=ALL_WHEELS_EXCEED_LIMIT)

    fitting.sort(key=lambda a: ("none-any" not in a.filename, a.filename))
    chosen = fitting[0]
    return WheelSelection(
        candidate=WheelCandidate(
            filename=chosen.filename,
            download_url=chosen.download_url,
            size_bytes=chosen.size_bytes,
        ),
        error=None,
    )


NO_TARBALL_AVAILABLE = "no tarball available"
TARBALL_EXCEEDS_LIMIT = "tarball too large (unpacked)"


def select_npm_tarball(artifacts: Iterable, max_unpacked_bytes: int) -> WheelSelection:
    """npm version ships a single tgz; the size gate uses dist.unpackedSize
    (stored in platform_tags) — the same pre-download check classifire used."""
    tarballs = [a for a in artifacts if a.artifact_type == "tgz"]
    if not tarballs:
        return WheelSelection(candidate=None, error=NO_TARBALL_AVAILABLE)
    chosen = sorted(tarballs, key=lambda a: a.filename)[0]
    unpacked = (getattr(chosen, "platform_tags", None) or {}).get("unpacked_size") or 0
    if unpacked > max_unpacked_bytes:
        return WheelSelection(candidate=None, error=TARBALL_EXCEEDS_LIMIT)
    return WheelSelection(
        candidate=WheelCandidate(
            filename=chosen.filename,
            download_url=chosen.download_url,
            size_bytes=chosen.size_bytes,
        ),
        error=None,
    )
