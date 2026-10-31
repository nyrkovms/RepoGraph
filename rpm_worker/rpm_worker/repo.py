"""Repository metadata on the worker's volume (SPEC-MULTI-ECOSYSTEM.md §2.2, §6.4, §9.8.1).

A source is one repository of one release; its `base_url` may carry `$basearch` and `$releasever`, expanded for every
arch of `config.arch` (one cache per source and arch). A sync reads `repodata/repomd.xml` (conditional on its ETag),
and when the file changed downloads `primary`, `filelists` and `modules` (when listed), checks their checksums, and
writes the `.solv` libsolv loads quickly. The revision of a repository is a hash of its `repomd.xml`: the `<revision>`
element is not reliable across vendors (Rocky 8 writes `8.10` for months).

Layout: `<RPM_CACHE_DIR>/<registry id>/<arch>/<repomd sha>/{repomd.xml, primary..., filelists..., modules..., repo.solv}`
and `state.json` next to the snapshot directories naming the current one; the previous snapshot is kept one generation
(a task may still read it) and older ones are removed. Syncs of one source and arch are serialized with a file lock.
A repository without repomd.xml is an error of the source (SourceError), never an empty source.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from rpm_worker import http

logger = logging.getLogger(__name__)

_REPO_NS = "{http://linux.duke.edu/metadata/repo}"
WANTED = ("primary", "filelists", "modules")


@dataclass
class Source:
    id: str
    slug: str
    base_url: str
    namespace: str
    arches: List[str]
    releasever: str = ""
    modular: bool = False
    auth_type: str = "none"
    credentials_ref: Optional[str] = None

    def url(self, arch: str) -> str:
        url = self.base_url.replace("$basearch", arch).replace("$releasever", self.releasever)
        return url if url.endswith("/") else url + "/"


def source_from(raw: Mapping[str, Any]) -> Source:
    """A source as the API sends it: the registry row's fields and its `config` (§6.4)."""
    config = raw.get("config") or {}
    arches = config.get("arch") or ["x86_64"]
    return Source(
        id=str(raw["id"]), slug=str(raw["slug"]), base_url=str(raw["base_url"]),
        namespace=str(config.get("namespace") or ""), arches=[str(a) for a in arches],
        releasever=str(config.get("releasever") or config.get("release") or ""),
        modular=bool(config.get("modular", False)), auth_type=str(raw.get("auth_type") or "none"),
        credentials_ref=raw.get("credentials_ref")
    )


@dataclass
class DataFile:
    kind: str
    href: str
    checksum: str
    checksum_type: str


@dataclass
class Snapshot:
    """The current metadata of one source and arch."""
    directory: Path
    revision: str
    etag: Optional[str]
    files: Dict[str, str] = field(default_factory=dict)       # kind -> file name in the directory
    synced_at: float = 0.0
    state: Dict[str, Any] = field(default_factory=dict)

    def path(self, kind: str) -> Optional[Path]:
        name = self.files.get(kind)
        return self.directory / name if name else None

    @property
    def solv(self) -> Path:
        return self.directory / "repo.solv"


def parse_repomd(content: bytes) -> List[DataFile]:
    root = ElementTree.fromstring(content)
    out: List[DataFile] = []
    for data in root.findall(_REPO_NS + "data"):
        location, checksum = data.find(_REPO_NS + "location"), data.find(_REPO_NS + "checksum")
        if location is None or checksum is None or not location.get("href"):
            continue
        out.append(DataFile(str(data.get("type")), str(location.get("href")), (checksum.text or "").strip(),
                            str(checksum.get("type") or "sha256")))
    return out


def cache_root() -> Path:
    return Path(os.environ.get("RPM_CACHE_DIR", "/var/cache/rpm-worker"))


def _state_path(source: Source, arch: str) -> Path:
    return cache_root() / source.id / arch / "state.json"


def current(source: Source, arch: str) -> Optional[Snapshot]:
    path = _state_path(source, arch)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    found = Snapshot(path.parent / state["directory"], state["revision"], state.get("etag"), state["files"],
                     float(state.get("synced_at") or 0), state)
    return found if found.solv.exists() else None


def _checksum(path: Path, kind: str) -> str:
    digest = hashlib.new("sha1" if kind == "sha" else kind)
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(client: httpx.Client, url: str, target: Path) -> None:
    response = http.get(client, url)
    if response.status_code in (404, 410):
        raise SourceError("%s is listed in repomd.xml but missing" % url)
    target.write_bytes(response.content)


class SourceError(Exception):
    """The repository could not be read: no repomd.xml there (404/410: a wrong URL, a release moved to the vault),
    broken metadata. An error of the source, never "the package is not there" (SPEC §6.4: absence is only absence
    in repodata that was read)."""


# A snapshot younger than this answers interactive tasks without asking the source; sync_repo and detect_changes
# refresh regardless.
FRESH_SECONDS = int(os.environ.get("RPM_FRESH_SECONDS", "3600"))


@contextlib.contextmanager
def _locked(source: Source, arch: str) -> Iterator[None]:
    """One sync per source and arch at a time, across the worker's processes."""
    path = _state_path(source, arch).with_name("sync.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def snapshot(source: Source, arch: str, client: Optional[httpx.Client] = None, refresh: bool = False) -> Snapshot:
    """The source's snapshot for one arch: the cached one while fresh (and when the source is down but a snapshot
    exists, for interactive tasks), else refreshed."""
    known = current(source, arch)
    if known is not None and not refresh and time.time() - known.synced_at < FRESH_SECONDS:
        return known
    try:
        return sync(source, arch, client)
    except (httpx.HTTPError, http.SourceAuthError):
        if known is not None and not refresh:
            logger.warning("%s/%s: source unreachable, answering from the snapshot of %s", source.slug, arch,
                           time.strftime("%Y-%m-%d %H:%M", time.gmtime(known.synced_at)))
            return known
        raise


def sync(source: Source, arch: str, client: Optional[httpx.Client] = None) -> Snapshot:
    """The current snapshot of the source for one arch, refreshed when repomd.xml changed."""
    own = client is None
    client = client or http.make_client(auth_type=source.auth_type, credentials_ref=source.credentials_ref)
    try:
        with _locked(source, arch):
            return _sync(source, arch, client)
    finally:
        if own:
            client.close()


def _write_state(source: Source, arch: str, state: Dict[str, Any]) -> None:
    path = _state_path(source, arch)
    tmp = path.with_name(".state.json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(str(tmp), str(path))


def _sync(source: Source, arch: str, client: httpx.Client) -> Snapshot:
    known = current(source, arch)
    base = source.url(arch)
    headers = {"If-None-Match": known.etag} if known is not None and known.etag else {}
    response = http.get(client, base + "repodata/repomd.xml", headers)
    if response.status_code in (404, 410):
        raise SourceError("%s: no repodata/repomd.xml (%d)" % (base, response.status_code))
    if known is not None and (response.status_code == 304
                              or hashlib.sha256(response.content).hexdigest() == known.revision):
        _write_state(source, arch, dict(known.state, synced_at=time.time()))
        return current(source, arch) or known
    content = response.content
    revision = hashlib.sha256(content).hexdigest()
    directory = _state_path(source, arch).parent / revision[:16]
    building = directory.with_name(directory.name + ".partial")
    shutil.rmtree(str(building), ignore_errors=True)
    building.mkdir(parents=True)
    (building / "repomd.xml").write_bytes(content)
    files: Dict[str, str] = {}
    for data in parse_repomd(content):
        if data.kind not in WANTED:
            continue
        name = data.href.rsplit("/", 1)[-1]
        _download(client, base + data.href, building / name)
        if data.checksum and _checksum(building / name, data.checksum_type) != data.checksum:
            raise SourceError("%s: checksum mismatch for %s" % (base, data.href))
        files[data.kind] = name
    if "primary" not in files:
        raise SourceError("%s: repomd.xml lists no primary" % base)
    from rpm_worker import pools          # libsolv is imported only where it is needed
    pools.write_solv(building, files)
    shutil.rmtree(str(directory), ignore_errors=True)
    os.replace(str(building), str(directory))
    state = {"directory": directory.name, "revision": revision, "etag": response.headers.get("etag"), "files": files,
             "synced_at": time.time(), "previous": known.directory.name if known is not None else None}
    _write_state(source, arch, state)
    # The previous snapshot stays one generation: a task that loaded it a moment ago keeps reading its files.
    keep = {directory.name, state["previous"]}
    for old in _state_path(source, arch).parent.iterdir():
        if old.is_dir() and old.name not in keep and not old.name.endswith(".partial"):
            shutil.rmtree(str(old), ignore_errors=True)
    return current(source, arch) or Snapshot(directory, revision, state["etag"], files)


def revision(source: Source, client: Optional[httpx.Client] = None) -> str:
    """The source's revision across its arches without downloading data files; SourceError without a repomd.xml."""
    own = client is None
    client = client or http.make_client(auth_type=source.auth_type, credentials_ref=source.credentials_ref)
    try:
        parts = []
        for arch in source.arches:
            response = http.get(client, source.url(arch) + "repodata/repomd.xml")
            if response.status_code in (404, 410):
                raise SourceError("%s: no repodata/repomd.xml (%d)" % (source.url(arch), response.status_code))
            parts.append(hashlib.sha256(response.content).hexdigest())
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()
    finally:
        if own:
            client.close()
