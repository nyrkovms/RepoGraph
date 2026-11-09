"""Author fields the source publishes are free text and do not always fit the columns."""

import asyncio
from types import SimpleNamespace
from typing import Any

from app.fetcher.pipeline import _fit_author_field, _upsert_authors
from app.models.author import AUTHOR_FIELD_MAX_LENGTH, Author


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.lookups: list[dict[str, Any]] = []

    async def execute(self, _statement: Any) -> None:
        return None

    async def scalar(self, statement: Any) -> None:
        self.lookups.append(statement.compile().params)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _write(author: SimpleNamespace) -> _Session:
    session = _Session()
    asyncio.run(_upsert_authors(session, "package-id", [author]))  # type: ignore[arg-type]
    return session


def test_an_address_list_over_the_column_keeps_its_whole_addresses() -> None:
    """Regression (#77): aioftp publishes 445 characters of author_email, its sync failed on the insert."""
    addresses = [f"person{i:03}@example.org" for i in range(30)]
    session = _write(SimpleNamespace(name="The aioftp team", email=", ".join(addresses), url=None, role="author"))

    author = next(row for row in session.added if isinstance(row, Author))
    assert len(author.email) <= AUTHOR_FIELD_MAX_LENGTH
    assert author.email.split(", ") == addresses[:len(author.email.split(", "))]


def test_a_stored_author_is_looked_up_by_the_value_that_fits() -> None:
    """Looking up the full value would never find the row and add a new author on every sync."""
    long_name = ", ".join(f"Person Number{i:03}" for i in range(40))
    session = _write(SimpleNamespace(name=long_name, email=None, url=None, role="author"))

    author = next(row for row in session.added if isinstance(row, Author))
    assert session.lookups[0]["name_1"] == author.name
    assert len(author.name) <= AUTHOR_FIELD_MAX_LENGTH


def test_a_value_without_separators_is_cut_at_the_column_length() -> None:
    assert _fit_author_field("x" * 300) == "x" * AUTHOR_FIELD_MAX_LENGTH


def test_values_that_fit_stay_as_they_are() -> None:
    assert _fit_author_field("A, B") == "A, B"
    assert _fit_author_field("x" * AUTHOR_FIELD_MAX_LENGTH) == "x" * AUTHOR_FIELD_MAX_LENGTH
    assert _fit_author_field(None) is None
