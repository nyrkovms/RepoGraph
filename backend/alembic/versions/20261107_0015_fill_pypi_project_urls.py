"""Fill PyPI homepage and documentation from project_urls (#69)

The connector read only the legacy `info.home_page` and `info.docs_url`, which modern packages leave empty. It now falls
back to `project_urls`; a daily run does not refresh a package without a new release, so stored rows are filled here
from the stored document. Only NULL columns of PyPI packages change, so a second run changes nothing and a value the
source did give is never overwritten. The rule repeats `_link` of the PyPI connector on purpose: a migration does not
import application code that may change later. The downgrade keeps the filled values.

Revision ID: 20261107_0015
Revises: 20261106_0014
Create Date: 2026-11-07
"""

from collections.abc import Mapping, Sequence
from typing import Any, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20261107_0015"
down_revision: Union[str, Sequence[str], None] = "20261106_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CANDIDATES = sa.text("""
    SELECT id, homepage_url IS NULL AS need_homepage, documentation_url IS NULL AS need_docs,
           raw_metadata -> 'info' -> 'project_urls' AS project_urls
    FROM packages
    WHERE ecosystem = 'pypi'
      AND (homepage_url IS NULL OR documentation_url IS NULL)
      AND jsonb_typeof(raw_metadata -> 'info' -> 'project_urls') = 'object'
""")

_UPDATE = sa.text("""
    UPDATE packages
    SET homepage_url = COALESCE(homepage_url, :homepage), documentation_url = COALESCE(documentation_url, :docs)
    WHERE id = :id
""")


def pick(project_urls: Any, *keys: str) -> str | None:
    """The first of `keys` found in `project_urls`, keys compared without case, empty strings as missing."""
    if not isinstance(project_urls, Mapping):
        return None
    by_key = {key.lower(): value.strip() for key, value in project_urls.items()
              if isinstance(key, str) and isinstance(value, str) and value.strip()}
    return next((by_key[key] for key in keys if key in by_key), None)


def upgrade() -> None:
    bind = op.get_bind()
    updates = []
    for row in bind.execute(_CANDIDATES).mappings():
        homepage = pick(row["project_urls"], "homepage") if row["need_homepage"] else None
        docs = pick(row["project_urls"], "documentation", "docs") if row["need_docs"] else None
        if homepage or docs:
            updates.append({"id": row["id"], "homepage": homepage, "docs": docs})
    if updates:
        bind.execute(_UPDATE, updates)


def downgrade() -> None:
    pass
