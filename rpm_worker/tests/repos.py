"""Synthetic rpm-md repositories for the worker's tests: `primary.xml.gz` (and `filelists`) written from a few package
dicts, `repomd.xml` with their checksums, served by a local HTTP server with ETags and optional basic auth."""

from __future__ import annotations

import base64
import gzip
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

COMMON = "http://linux.duke.edu/metadata/common"
RPM = "http://linux.duke.edu/metadata/rpm"


def _entry(cap: Any) -> str:
    if isinstance(cap, str):
        return "<rpm:entry name=%s/>" % quoteattr(cap)
    name, flags, evr = cap
    epoch, _, rest = evr.rpartition(":") if ":" in evr else ("0", "", evr)
    ver, _, rel = rest.partition("-")
    attrs = 'name=%s flags="%s" epoch="%s" ver=%s' % (quoteattr(name), flags, epoch or "0", quoteattr(ver))
    if rel:
        attrs += " rel=%s" % quoteattr(rel)
    return "<rpm:entry %s/>" % attrs


def _package(p: Mapping[str, Any]) -> str:
    epoch, _, rest = p["evr"].rpartition(":") if ":" in p["evr"] else ("0", "", p["evr"])
    ver, _, rel = rest.partition("-")
    name, arch = p["name"], p.get("arch", "x86_64")
    pkgid = hashlib.sha256(("%s-%s.%s" % (name, p["evr"], arch)).encode()).hexdigest()
    sections = ""
    provides = list(p.get("provides", [])) + [(name, "EQ", p["evr"])]
    for kind, caps in (("provides", provides), ("requires", p.get("requires", [])),
                       ("recommends", p.get("recommends", [])), ("suggests", p.get("suggests", []))):
        if caps:
            sections += "<rpm:%s>%s</rpm:%s>" % (kind, "".join(_entry(c) for c in caps), kind)
    files = "".join("<file>%s</file>" % escape(f) for f in p.get("files", []))
    return (
        '<package type="rpm"><name>%s</name><arch>%s</arch><version epoch="%s" ver="%s" rel="%s"/>'
        '<checksum type="sha256" pkgid="YES">%s</checksum><summary>%s</summary><description>%s</description>'
        '<packager/><url>%s</url><time file="1" build="%d"/><size package="1000" installed="2000" archive="3000"/>'
        '<location href="Packages/%s-%s-%s.%s.rpm"/><format><rpm:license>%s</rpm:license>'
        '<rpm:sourcerpm>%s-%s-%s.src.rpm</rpm:sourcerpm>%s%s</format></package>'
        % (escape(name), arch, epoch or "0", escape(ver), escape(rel), pkgid, escape(p.get("summary", name)),
           escape(p.get("description", "")), escape(p.get("url", "")), p.get("buildtime", 1700000000),
           name, ver, rel, arch, escape(p.get("license", "MIT")), name, ver, rel, sections, files))


def write_repo(directory: Path, packages: List[Mapping[str, Any]], modules: Optional[bytes] = None) -> None:
    """`<directory>/repodata/{repomd.xml, <sha>-primary.xml.gz, ...}`."""
    repodata = directory / "repodata"
    repodata.mkdir(parents=True, exist_ok=True)
    for old in repodata.iterdir():
        old.unlink()
    primary = ('<?xml version="1.0" encoding="UTF-8"?><metadata xmlns="%s" xmlns:rpm="%s" packages="%d">%s</metadata>'
               % (COMMON, RPM, len(packages), "".join(_package(p) for p in packages))).encode()
    entries = [("primary", "primary.xml.gz", gzip.compress(primary))]
    if modules is not None:
        entries.append(("modules", "modules.yaml.gz", gzip.compress(modules)))
    data = ""
    # Fixed gzip timestamps: the same packages give the same files (and recorded fixtures do not churn).
    entries = [(k, n, gzip.compress(gzip.decompress(c), mtime=0)) for k, n, c in entries]
    for kind, suffix, content in entries:
        digest = hashlib.sha256(content).hexdigest()
        name = "%s-%s" % (digest, suffix)
        (repodata / name).write_bytes(content)
        data += ('<data type="%s"><checksum type="sha256">%s</checksum><location href="repodata/%s"/>'
                 '<size>%d</size></data>' % (kind, digest, name, len(content)))
    (repodata / "repomd.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><repomd xmlns="http://linux.duke.edu/metadata/repo">'
        "<revision>1</revision>%s</repomd>" % data)


class Server:
    """Serves a directory tree; ETag = sha256 of the file; counts requests; optional basic auth."""

    def __init__(self, root: Path, credentials: Optional[Tuple[str, str]] = None) -> None:
        self.root, self.credentials = root, credentials
        self.requests: List[Tuple[str, str, int]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                status, headers, body = server.answer(self.path, dict(self.headers))
                server.requests.append(("GET", self.path, status))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def answer(self, path: str, headers: Dict[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        if self.credentials is not None:
            wanted = "Basic " + base64.b64encode(("%s:%s" % self.credentials).encode()).decode()
            if headers.get("Authorization") != wanted:
                return 401, {}, b""
        target = self.root / path.lstrip("/")
        if not target.is_file():
            return 404, {}, b""
        body = target.read_bytes()
        etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
        if headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag}, b""
        return 200, {"ETag": etag}, body

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.httpd.server_address[1]

    def __enter__(self) -> "Server":
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.httpd.shutdown()
