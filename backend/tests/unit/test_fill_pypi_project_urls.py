"""The rule of the migration that fills PyPI homepage and documentation from project_urls (#69)."""

import importlib.util
from pathlib import Path

from app.ecosystems.pypi.connector import _link

_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "20261107_0015_fill_pypi_project_urls.py"
_SPEC = importlib.util.spec_from_file_location("fill_pypi_project_urls", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)

DJANGO = {"Homepage": "https://www.djangoproject.com/", "Documentation": "https://docs.djangoproject.com/"}


def test_pick_matches_keys_without_case() -> None:
    assert migration.pick(DJANGO, "homepage") == "https://www.djangoproject.com/"
    assert migration.pick({"DOCS": "https://d"}, "documentation", "docs") == "https://d"


def test_pick_ignores_empty_values_and_foreign_shapes() -> None:
    assert migration.pick({"Homepage": ""}, "homepage") is None
    assert migration.pick(None, "homepage") is None
    assert migration.pick(["Homepage"], "homepage") is None


def test_the_rule_is_the_same_as_the_connectors() -> None:
    for urls in (DJANGO, {"homepage": " "}, {"Docs": "https://d", "Documentation": "https://e"}, {}):
        assert migration.pick(urls, "homepage") == _link(None, urls, "homepage")
        assert migration.pick(urls, "documentation", "docs") == _link(None, urls, "documentation", "docs")
