import io
import tarfile
from dataclasses import dataclass, field

from app.probe.extract_npm import extract_tarball_facts
from app.probe.selection import (
    NO_TARBALL_AVAILABLE,
    TARBALL_EXCEEDS_LIMIT,
    select_npm_tarball,
)

MAX = 30_000_000


def build_tarball(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, content in members.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_extracts_index_js_and_readme():
    payload = build_tarball(
        {
            "package/package.json": "{}",
            "package/index.js": "module.exports = {}",
            "package/README.md": "# toolkit\n\nDoes things.",
        }
    )
    facts = extract_tarball_facts(payload)
    assert facts.error is None
    assert facts.file_facts["has_index_js"] is True
    assert facts.file_facts["total"] == 3
    assert facts.readme.startswith("# toolkit")


def test_no_index_js_no_readme():
    payload = build_tarball({"package/lib/main.cjs": ""})
    facts = extract_tarball_facts(payload)
    assert facts.file_facts["has_index_js"] is False
    assert facts.readme is None


def test_readme_case_insensitive():
    payload = build_tarball({"package/ReadMe.MARKDOWN": "hello"})
    facts = extract_tarball_facts(payload)
    assert facts.readme == "hello"


def test_bad_tarball_is_permanent_error():
    facts = extract_tarball_facts(b"definitely not a tarball")
    assert facts.error is not None and facts.error.startswith("tarball parse error")


@dataclass
class FakeArtifact:
    filename: str
    download_url: str = "https://cdn/x.tgz"
    size_bytes: int | None = None
    artifact_type: str = "tgz"
    platform_tags: dict | None = field(default_factory=dict)


def test_select_single_tarball():
    result = select_npm_tarball([FakeArtifact("toolkit-2.0.0.tgz", platform_tags={"unpacked_size": 1000})], MAX)
    assert result.error is None
    assert result.candidate.filename == "toolkit-2.0.0.tgz"


def test_select_rejects_oversized_unpacked():
    result = select_npm_tarball([FakeArtifact("big-1.0.0.tgz", platform_tags={"unpacked_size": MAX + 1})], MAX)
    assert result.candidate is None
    assert result.error == TARBALL_EXCEEDS_LIMIT


def test_select_no_tarball():
    result = select_npm_tarball([], MAX)
    assert result.error == NO_TARBALL_AVAILABLE


def test_select_missing_unpacked_size_passes():
    result = select_npm_tarball([FakeArtifact("x-1.0.0.tgz", platform_tags=None)], MAX)
    assert result.error is None
