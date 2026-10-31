"""Synthetic repositories written straight into the API's replay fixture tree (`backend/tests/fixtures/http`), on a host
that does not exist, so integration tests run the real rpm-worker offline (`python3 -m tests.fixture_repos <dir>`).

Real repodata is tens of MB per repository (IMPL E6 probes); these hold a handful of packages shaped like Rocky's:
rocky-9 BaseOS for x86_64 and aarch64 (openssl with a superseded build, its libraries, bash for /bin/sh, a noarch
package, a weak dependency) and rocky-8 AppStream with nodejs streams 18 (default) and 20 and a non-modular nodejs, and epel-9 with a package that
needs rocky-9's glibc.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import httpx

from rpm_worker.http import recording_key
from tests.repos import write_repo

HOST = "rpm.fixtures.packagehub.test"
BASE = "https://%s/" % HOST

GLIBC = {"name": "glibc", "evr": "2.34-100.el9", "provides": ["libc.so.6()(64bit)", "rtld(GNU_HASH)"]}
BASH = {"name": "bash", "evr": "5.1.8-9.el9", "requires": ["libc.so.6()(64bit)"], "files": ["/bin/sh", "/usr/bin/sh"]}
LIBS = {"name": "openssl-libs", "evr": "1:3.0.7-27.el9", "requires": ["libc.so.6()(64bit)"],
        "provides": ["libssl.so.3()(64bit)", "libcrypto.so.3()(64bit)"]}
POLICIES = {"name": "crypto-policies", "evr": "20240304-1.git0d3d7d7.el9", "arch": "noarch", "license": "LGPL-2.1-or-later"}


def openssl(evr: str, arch: str) -> dict:
    return {"name": "openssl", "evr": evr, "arch": arch, "license": "Apache-2.0", "url": "http://www.openssl.org/",
            "summary": "Utilities from the general purpose cryptography library with TLS implementation",
            "requires": ["libssl.so.3()(64bit)", "libcrypto.so.3()(64bit)", "/bin/sh",
                         ("openssl-libs(x86-64)" if arch == "x86_64" else "openssl-libs(aarch-64)", "EQ", evr)],
            "recommends": ["crypto-policies"], "buildtime": 1714000000 if evr.endswith("26.el9") else 1720000000}


def libs(arch: str) -> dict:
    return dict(LIBS, arch=arch, provides=LIBS["provides"] + [
        "openssl-libs(x86-64)" if arch == "x86_64" else "openssl-libs(aarch-64)"])


def rocky9(arch: str) -> list:
    return [dict(GLIBC, arch=arch), dict(BASH, arch=arch), libs(arch), POLICIES,
            openssl("1:3.0.7-26.el9", arch), openssl("1:3.0.7-27.el9", arch)]


NODE_MODULES = b"""---
document: modulemd
version: 2
data:
  name: nodejs
  stream: "18"
  version: 8100020240807
  context: ad008a3a
  arch: x86_64
  summary: Javascript runtime
  description: Node.js
  license:
    module: [MIT]
  dependencies:
  - buildrequires: {platform: [el8]}
    requires: {platform: [el8]}
  profiles:
    common:
      rpms: [nodejs, npm]
  artifacts:
    rpms:
    - nodejs-1:18.20.4-1.module+el8.10.0+1.x86_64
    - npm-1:10.7.0-1.18.20.4.1.module+el8.10.0+1.x86_64
...
---
document: modulemd
version: 2
data:
  name: nodejs
  stream: "20"
  version: 8100020240808
  context: ad008a3a
  arch: x86_64
  summary: Javascript runtime
  description: Node.js
  license:
    module: [MIT]
  dependencies:
  - buildrequires: {platform: [el8]}
    requires: {platform: [el8]}
  profiles:
    common:
      rpms: [nodejs, npm]
  artifacts:
    rpms:
    - nodejs-1:20.16.0-1.module+el8.10.0+2.x86_64
    - npm-1:10.8.1-1.20.16.0.1.module+el8.10.0+2.x86_64
...
---
document: modulemd-defaults
version: 1
data:
  module: nodejs
  stream: "18"
  profiles:
    "18": [common]
    "20": [common]
...
"""

ROCKY8 = [
    {"name": "glibc", "evr": "2.28-251.el8", "provides": ["libc.so.6()(64bit)"], "files": ["/bin/sh"]},
    {"name": "nodejs", "evr": "1:10.24.0-1.el8", "requires": ["libc.so.6()(64bit)"]},
    {"name": "nodejs", "evr": "1:18.20.4-1.module+el8.10.0+1", "requires": ["libc.so.6()(64bit)"]},
    {"name": "npm", "evr": "1:10.7.0-1.18.20.4.1.module+el8.10.0+1",
     "requires": [("nodejs", "EQ", "1:18.20.4-1.module+el8.10.0+1")]},
    {"name": "nodejs", "evr": "1:20.16.0-1.module+el8.10.0+2", "requires": ["libc.so.6()(64bit)"]},
    {"name": "npm", "evr": "1:10.8.1-1.20.16.0.1.module+el8.10.0+2",
     "requires": [("nodejs", "EQ", "1:20.16.0-1.module+el8.10.0+2")]},
]


def _record(out: Path, url: str, body: bytes) -> None:
    request = httpx.Request("GET", url, headers={"accept": "*/*"})
    directory = out / HOST
    directory.mkdir(parents=True, exist_ok=True)
    key = recording_key(request)
    etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
    meta = {"request": {"method": "GET", "url": url, "headers": {"accept": "*/*"}}, "status": 200,
            "headers": {"etag": etag, "content-type": "application/octet-stream"}}
    (directory / (key + ".body.gz")).write_bytes(gzip.compress(body, mtime=0))
    (directory / (key + ".json")).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def _publish(out: Path, prefix: str, packages: list, modules: bytes = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_repo(root, packages, modules)
        for path in sorted((root / "repodata").iterdir()):
            _record(out, BASE + prefix + "repodata/" + path.name, path.read_bytes())


EPEL9 = [{"name": "htop", "evr": "3.3.0-1.el9", "requires": ["libc.so.6()(64bit)"], "license": "GPL-2.0-or-later"}]


def main(out: Path) -> None:
    for arch in ("x86_64", "aarch64"):
        _publish(out, "rocky-9/BaseOS/%s/os/" % arch, rocky9(arch))
    _publish(out, "rocky-8/AppStream/x86_64/os/", ROCKY8, NODE_MODULES)
    _publish(out, "epel/9/Everything/x86_64/", EPEL9)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
