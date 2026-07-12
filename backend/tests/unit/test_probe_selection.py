from dataclasses import dataclass

from app.probe.selection import (
    ALL_WHEELS_EXCEED_LIMIT,
    NO_WHEEL_AVAILABLE,
    select_representative_wheel,
)

MAX = 20_000_000


@dataclass
class FakeArtifact:
    filename: str
    download_url: str
    size_bytes: int | None
    artifact_type: str


def wheel(filename: str, size: int | None = 1000) -> FakeArtifact:
    return FakeArtifact(filename=filename, download_url=f"https://cdn/{filename}", size_bytes=size, artifact_type="wheel")


def sdist(filename: str = "pkg-1.0.tar.gz") -> FakeArtifact:
    return FakeArtifact(filename=filename, download_url=f"https://cdn/{filename}", size_bytes=500, artifact_type="sdist")


def test_prefers_cross_platform_wheel():
    result = select_representative_wheel(
        [wheel("pkg-1.0-cp312-cp312-manylinux_x86_64.whl"), wheel("pkg-1.0-py3-none-any.whl"), sdist()],
        MAX,
    )
    assert result.error is None
    assert result.candidate.filename == "pkg-1.0-py3-none-any.whl"
    assert result.candidate.platform_wheel is False


def test_platform_wheel_when_no_universal():
    result = select_representative_wheel(
        [wheel("pkg-1.0-cp312-cp312-manylinux_x86_64.whl")],
        MAX,
    )
    assert result.error is None
    assert result.candidate.filename == "pkg-1.0-cp312-cp312-manylinux_x86_64.whl"
    assert result.candidate.platform_wheel is True


def test_no_wheel_available():
    result = select_representative_wheel([sdist()], MAX)
    assert result.candidate is None
    assert result.error == NO_WHEEL_AVAILABLE


def test_all_wheels_exceed_limit():
    result = select_representative_wheel([wheel("pkg-1.0-py3-none-any.whl", size=MAX + 1)], MAX)
    assert result.candidate is None
    assert result.error == ALL_WHEELS_EXCEED_LIMIT


def test_oversized_wheel_skipped_in_favor_of_fitting_one():
    result = select_representative_wheel(
        [wheel("pkg-1.0-py3-none-any.whl", size=MAX + 1), wheel("pkg-1.0-cp312-cp312-manylinux_x86_64.whl")],
        MAX,
    )
    assert result.error is None
    assert result.candidate.filename == "pkg-1.0-cp312-cp312-manylinux_x86_64.whl"


def test_unknown_size_passes_filter():
    result = select_representative_wheel([wheel("pkg-1.0-py3-none-any.whl", size=None)], MAX)
    assert result.error is None
    assert result.candidate.size_bytes is None
