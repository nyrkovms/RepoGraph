"""npm tarball fact extraction (port of classifire npm_probe).

In-memory tarfile reading: names listing + README member only, never
extractall. README goes to version_descriptions (origin tarball-readme).
"""

import io
import re
import tarfile
from dataclasses import dataclass

from app.probe.extract import DESCRIPTION_MAX_CHARS, ext_counts

INDEX_JS_NAMES = ("package/index.js", "package/index.cjs", "package/index.mjs", "package/index.node")
README_PATTERN = re.compile(r"package/readme(\.(md|markdown|txt|rst))?", re.IGNORECASE)


@dataclass(frozen=True)
class TarballFacts:
    file_facts: dict | None
    readme: str | None
    error: str | None


def extract_tarball_facts(payload: bytes) -> TarballFacts:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            names = tf.getnames()
            file_facts = {
                "total": len(names),
                "ext_counts": ext_counts(names),
                "has_index_js": any(candidate in names for candidate in INDEX_JS_NAMES),
            }
            readme: str | None = None
            readme_names = [n for n in names if README_PATTERN.fullmatch(n)]
            if readme_names:
                member = tf.extractfile(readme_names[0])
                if member:
                    readme = member.read(DESCRIPTION_MAX_CHARS).decode("utf-8", errors="replace")
    except (tarfile.TarError, EOFError, OSError) as error:
        return TarballFacts(file_facts=None, readme=None, error=f"tarball parse error: {error}")
    return TarballFacts(file_facts=file_facts, readme=readme, error=None)
