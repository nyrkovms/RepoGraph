import uuid

from app.models.enums import DependencyType as T
from app.services.usage import summarize_dependencies

A, B, C = (uuid.uuid4() for _ in range(3))


def key(name: str) -> tuple[str, str, str]:
    return ("npm", "", name)


def test_a_name_counts_once_in_the_first_group() -> None:
    summary = summarize_dependencies([
        (key("a"), T.runtime, A), (key("a"), T.optional, A), (key("a"), T.dev, A),   # direct wins
        (key("b"), T.optional, B), (key("b"), T.dev, B),                             # optional wins over dev
        (key("c"), T.build, None), (key("d"), T.dev, None),
        (key("e"), T.peer, C), (key("f"), T.provided, None),
    ])
    assert (summary.direct, summary.optional, summary.dev) == (3, 1, 2)
    assert summary.direct_ids == {A, C}


def test_not_in_catalog_covers_direct_and_optional_names_only() -> None:
    summary = summarize_dependencies([
        (key("a"), T.runtime, A), (key("x"), T.runtime, None), (key("y"), T.optional, None),
        (key("z"), T.dev, None), (key("x"), T.peer, None),
    ])
    assert summary.not_in_catalog == {key("x"), key("y")}


def test_a_name_resolved_in_one_variant_is_in_the_catalog() -> None:
    summary = summarize_dependencies([(key("a"), T.runtime, None), (key("a"), T.runtime, A)])
    assert summary.direct == 1 and summary.not_in_catalog == frozenset()


def test_no_dependencies() -> None:
    summary = summarize_dependencies([])
    assert (summary.direct, summary.optional, summary.dev, summary.direct_ids) == (0, 0, 0, frozenset())


def test_a_dependency_on_the_package_itself_is_not_counted() -> None:
    me = uuid.uuid4()
    summary = summarize_dependencies(
        [(key("me"), T.runtime, me), (key("me"), T.optional, me), (key("a"), T.runtime, A), (key("me2"), T.dev, me)],
        self_id=me,
    )
    assert (summary.direct, summary.optional, summary.dev) == (1, 0, 0)
    assert summary.direct_ids == {A}


def test_without_self_id_nothing_is_dropped() -> None:
    assert summarize_dependencies([(key("a"), T.runtime, A)]).direct == 1
