import asyncio
import uuid

from sqlalchemy.dialects import postgresql

from app.tasks.seed import _unresolved_dependency_names


class _Result:
    def all(self) -> list[str]:
        return ["Werkzeug", "blinker"]


class _FakeDb:
    def __init__(self) -> None:
        self.statement = None

    async def scalars(self, statement):
        self.statement = statement
        return _Result()


def test_unresolved_dependency_names_follows_only_required_dependencies() -> None:
    db = _FakeDb()

    names = asyncio.run(_unresolved_dependency_names(db, uuid.uuid4()))

    assert names == ["werkzeug", "blinker"]
    compiled = db.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    assert "dependencies.dependency_type = 'runtime'" in str(compiled)
