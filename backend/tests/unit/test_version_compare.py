from app.services.version_compare import Added, Changed, DepRow, compare_dependencies


def dep(name: str, constraint: str, kind: str = "runtime", namespace: str = "") -> DepRow:
    # An npm-style scope: `@a/x`, the way the route builds the display name.
    return DepRow("npm", namespace, name, kind, constraint, display=f"@{namespace}/{name}" if namespace else name)


def test_added_removed_changed_and_the_unchanged_count() -> None:
    old = [dep("core", "==1"), dep("gone", ">=1"), dep("same", ">=2")]
    new = [dep("core", "==2"), dep("fresh", ">=3"), dep("same", ">=2")]
    result = compare_dependencies(old, new)
    assert result.added == [Added("fresh", ">=3", "runtime")]
    assert result.removed == [Added("gone", ">=1", "runtime")]
    assert result.changed == [Changed("core", "runtime", "==1", "==2")]
    assert result.unchanged == 1


def test_several_rows_for_a_key_are_sorted_distinct_constraints() -> None:
    old = [dep("x", ">=2"), dep("x", ">=1"), dep("x", ">=2")]
    new = [dep("x", ">=1"), dep("x", ">=2")]
    result = compare_dependencies(old, new)
    assert (result.changed, result.unchanged) == ([], 1)
    assert compare_dependencies([], old).added == [Added("x", ">=1, >=2", "runtime")]


def test_a_move_between_types_is_one_removed_and_one_added() -> None:
    result = compare_dependencies([dep("x", ">=1", "runtime")], [dep("x", ">=1", "dev")])
    assert result.removed == [Added("x", ">=1", "runtime")]
    assert result.added == [Added("x", ">=1", "dev")]
    assert (result.changed, result.unchanged) == ([], 0)


def test_the_namespace_is_part_of_the_key() -> None:
    result = compare_dependencies([dep("x", "1", namespace="a")], [dep("x", "1", namespace="b")])
    assert len(result.added) == 1 and len(result.removed) == 1
    # The two are different packages and read as different rows, not as `x` twice.
    assert [row.name for row in result.added] == ["@b/x"]
    assert [row.name for row in result.removed] == ["@a/x"]


def test_two_namespaces_with_one_bare_name_are_two_distinct_displayed_rows() -> None:
    old = [dep("core", "^1", namespace="babel"), dep("core", "^1", namespace="other"), dep("core", "^1")]
    new = [dep("core", "^2", namespace="babel"), dep("core", "^2", namespace="other"), dep("core", "^2")]
    names = [row.name for row in compare_dependencies(old, new).changed]
    assert sorted(names) == ["@babel/core", "@other/core", "core"]
    assert len(set(names)) == 3


def test_without_a_display_the_name_is_shown() -> None:
    result = compare_dependencies([], [DepRow("pypi", "", "requests", "runtime", ">=2")])
    assert result.added == [Added("requests", ">=2", "runtime")]


def test_the_result_does_not_depend_on_the_input_order() -> None:
    old = [dep("b", "1"), dep("a", "1", "dev"), dep("c", "1"), dep("d", "1")]
    new = [dep("c", "2"), dep("b", "2"), dep("e", "1", "dev"), dep("f", "1")]
    forward = compare_dependencies(old, new)
    assert forward == compare_dependencies(old[::-1], new[::-1])
    assert [c.name for c in forward.changed] == ["b", "c"]          # by type, then name
    assert [(a.dependency_type, a.name) for a in forward.added] == [("dev", "e"), ("runtime", "f")]
