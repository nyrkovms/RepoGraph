"""Offline fact extraction from a downloaded wheel (in-memory, no extractall).

Ported from classifire fetch.py. The archive is read strictly from BytesIO;
only known member names are extracted, so a hostile archive can at worst
fail parsing (recorded as a permanent probe error).
"""

import io
import zipfile
from collections import Counter
from dataclasses import dataclass

EXT_COUNT_LIMIT = 15
DESCRIPTION_MAX_CHARS = 500_000


@dataclass(frozen=True)
class WheelFacts:
    entry_points_txt: str | None
    top_level_txt: str | None
    file_facts: dict | None
    error: str | None
    # Body of *.dist-info/METADATA — the long description shipped inside the
    # distribution itself. Input for the classification layer; the only
    # description source for packages living in minimal-protocol registries.
    metadata_description: str | None = None


def ext_counts(names: list[str], limit: int = EXT_COUNT_LIMIT) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for name in names:
        base = name.rsplit("/", 1)[-1]
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else "<none>"
        counter[ext] += 1
    return dict(counter.most_common(limit))


def extract_wheel_facts(payload: bytes, wheel_filename: str) -> WheelFacts:
    entry_points_txt: str | None = None
    top_level_txt: str | None = None
    metadata_description: str | None = None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            file_facts = {
                "total": len(names),
                "ext_counts": ext_counts(names),
                "wheel": wheel_filename,
                # Scripts installed into bin/ besides console_scripts (ruff ships this way).
                "scripts": sorted({n.rsplit("/", 1)[-1] for n in names if "/scripts/" in n and ".data/" in n}),
                "platform_wheel": "none-any" not in wheel_filename,
            }
            for suffix in (".dist-info/entry_points.txt", ".dist-info/top_level.txt"):
                match = [n for n in names if n.endswith(suffix)]
                if not match:
                    continue
                content = zf.read(match[0]).decode("utf-8", errors="replace")
                if suffix.endswith("entry_points.txt"):
                    entry_points_txt = content
                else:
                    top_level_txt = content
            metadata_names = [n for n in names if n.endswith(".dist-info/METADATA")]
            if metadata_names:
                raw = zf.read(metadata_names[0]).decode("utf-8", errors="replace")
                # RFC 822: headers, blank line, then the long description body.
                _, separator, body = raw.partition("\n\n")
                if separator and body.strip():
                    metadata_description = body.strip()[:DESCRIPTION_MAX_CHARS]
    except (zipfile.BadZipFile, KeyError) as error:
        return WheelFacts(
            entry_points_txt=None,
            top_level_txt=None,
            file_facts=None,
            error=f"wheel parse error: {error}",
        )
    return WheelFacts(
        entry_points_txt=entry_points_txt,
        top_level_txt=top_level_txt,
        file_facts=file_facts,
        error=None,
        metadata_description=metadata_description,
    )
