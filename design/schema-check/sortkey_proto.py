"""Prototype byte-comparable sort keys: semver (npm/cargo/go), RPM EVR, PEP 440."""
import re
import sys


def _num(digits: str) -> bytes:
    digits = digits.lstrip("0") or "0"
    assert len(digits) < 256
    return bytes([len(digits)]) + digits.encode()


# --- SemVer 2.0 (npm, cargo, go) -------------------------------------------------------------
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def semver_key(v: str) -> bytes:
    m = _SEMVER.match(v)
    if not m:
        raise ValueError(v)
    out = _num(m[1]) + _num(m[2]) + _num(m[3])
    if m[4] is None:
        return out + b"\x02"                      # release sorts after every prerelease
    out += b"\x01"
    for ident in m[4].split("."):
        if ident.isdigit():
            out += b"\x01" + _num(ident)          # numeric identifiers < alphanumeric
        else:
            out += b"\x02" + ident.encode() + b"\x00"
    return out + b"\x00"                          # fewer identifiers sort first


# --- RPM EVR (rpmvercmp with ~ and ^) ----------------------------------------------------------
_RPM_TOKEN = re.compile(r"~|\^|[0-9]+|[A-Za-z]+")


def _rpm_part(s: str) -> bytes:
    out = b""
    for tok in _RPM_TOKEN.findall(s):
        if tok == "~":
            out += b"\x00"                        # tilde: older than anything, even end of string
        elif tok == "^":
            out += b"\x02"                        # caret: newer than end of string, older than a segment
        elif tok.isdigit():
            out += b"\x04" + _num(tok)            # numeric segment newer than alpha
        else:
            out += b"\x03" + tok.encode() + b"\x00"
    return out + b"\x01"                          # end of string


def rpm_key(evr: str) -> bytes:
    epoch, _, rest = evr.rpartition(":")
    version, _, release = rest.partition("-")
    return _num(epoch or "0") + _rpm_part(version) + _rpm_part(release)


# --- PEP 440 ---------------------------------------------------------------------------------
def pep440_key(v: str) -> bytes:
    from packaging.version import Version

    pv = Version(v)
    release = list(pv.release)
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    out = _num(str(pv.epoch))
    for part in release:
        out += b"\x02" + _num(str(part))
    out += b"\x01"
    if pv.pre is None and pv.post is None and pv.dev is not None:
        out += b"\x00"                            # 1.0.dev1 < 1.0a1
    elif pv.pre is None:
        out += b"\x02"
    else:
        out += b"\x01" + bytes([{"a": 1, "b": 2, "rc": 3}[pv.pre[0]]]) + _num(str(pv.pre[1]))
    out += b"\x00" if pv.post is None else b"\x01" + _num(str(pv.post))
    out += b"\x02" if pv.dev is None else b"\x01" + _num(str(pv.dev))
    if pv.local is None:
        out += b"\x00"
    else:
        out += b"\x01"
        for seg in pv.local.split("."):
            out += (b"\x02" + _num(seg)) if seg.isdigit() else (b"\x01" + seg.lower().encode() + b"\x00")
        out += b"\x00"
    return out


if __name__ == "__main__":
    scheme = sys.argv[1]
    fn = {"semver": semver_key, "rpm": rpm_key, "pep440": pep440_key}[scheme]
    for line in sys.stdin:
        v = line.strip()
        if v:
            print(f"{v}\t{fn(v).hex()}")
