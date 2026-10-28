import asyncio
import uuid
from enum import Enum

from sqlalchemy.dialects import postgresql

from app.ecosystems.base import PackageRef
from app.tasks.seed import _unresolved_dependency_refs


class _Eco(Enum):
    pypi = "pypi"
    npm = "npm"


class _Result:
    def all(self) -> list[tuple]:
        return [(_Eco.pypi, "", "Werkzeug"), (_Eco.pypi, None, "blinker"), (_Eco.npm, "@corp", "ui"), (_Eco.pypi, "", "")]


class _FakeDb:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result()


def test_unresolved_dependency_refs_follow_only_required_dependencies() -> None:
    db = _FakeDb()

    refs = asyncio.run(_unresolved_dependency_refs(db, uuid.uuid4()))

    # PyPI names are normalized, other ecosystems keep namespace and name; empty names are dropped.
    assert refs == [
        PackageRef("pypi", "", "werkzeug"),
        PackageRef("pypi", "", "blinker"),
        PackageRef("npm", "@corp", "ui"),
    ]
    compiled = db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    assert "dependency_type = 'runtime'" in str(compiled)
    assert "variant_registry_id IS NULL" in str(compiled)
