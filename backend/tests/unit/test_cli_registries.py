import pytest

from app import cli
from app.schemas.registries import ConnectionTestResponse, RegistryResponse, RegistryState
from app.services import registries as service
from app.services.registries import ImportResult


class _Session:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    monkeypatch.setattr(cli, "SessionLocal", _Session)


def test_parser_accepts_registries_subcommands() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["registries", "import", "registries.yaml"])
    assert (args.command, args.registries_command, args.file) == ("registries", "import", "registries.yaml")
    args = parser.parse_args(["registries", "test", "pypi"])
    assert (args.registries_command, args.slug) == ("test", "pypi")
    assert parser.parse_args(["registries", "list"]).ecosystem is None
    assert parser.parse_args(["registries", "list", "--ecosystem", "pypi"]).ecosystem == "pypi"


@pytest.mark.parametrize("argv", [["registries"], ["registries", "import"], ["registries", "list", "--ecosystem", "cobol"]])
def test_parser_rejects_incomplete_commands(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def test_existing_commands_still_parse() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["fetch", "flask"]).name == "flask"
    assert parser.parse_args(["refresh", "--limit", "5"]).limit == 5


def test_test_command_exit_code_follows_result(monkeypatch, capsys) -> None:
    results = iter([ConnectionTestResponse(ok=True, error=None, elapsed_ms=12.0),
                    ConnectionTestResponse(ok=False, error="ConnectError: refused", elapsed_ms=3.0)])

    async def fake(db, slug):
        return next(results)

    monkeypatch.setattr(service, "run_connection_test", fake)
    assert cli._run_registries(cli.build_parser().parse_args(["registries", "test", "pypi"])) == 0
    assert "ok" in capsys.readouterr().out
    assert cli._run_registries(cli.build_parser().parse_args(["registries", "test", "pypi"])) == 1
    assert "ConnectError: refused" in capsys.readouterr().out


def test_unknown_slug_is_an_error_exit(monkeypatch, capsys) -> None:
    async def fake(db, slug):
        raise service.RegistryNotFound(f"registry {slug!r} not found")

    monkeypatch.setattr(service, "run_connection_test", fake)
    assert cli._run_registries(cli.build_parser().parse_args(["registries", "test", "nope"])) == 1
    assert "not found" in capsys.readouterr().err


def test_import_prints_a_line_per_object_and_fails_on_errors(monkeypatch, tmp_path, capsys) -> None:
    path = tmp_path / "registries.yaml"
    path.write_text("registries: []\n", encoding="utf-8")
    outcomes = [[ImportResult("registry", "a", "created"), ImportResult("profile", "pypi/default", "unchanged")],
                [ImportResult("registry", "a", "created"), ImportResult("registry", "b", "error", "boom")]]

    async def fake(db, document):
        return outcomes.pop(0)

    monkeypatch.setattr(service, "import_document", fake)
    args = cli.build_parser().parse_args(["registries", "import", str(path)])
    assert cli._run_registries(args) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["created   registry a", "unchanged profile pypi/default"]
    assert cli._run_registries(args) == 1
    assert "error     registry b: boom" in capsys.readouterr().out


def test_import_of_a_missing_file_is_an_error_exit(capsys) -> None:
    args = cli.build_parser().parse_args(["registries", "import", "/nonexistent/registries.yaml"])
    assert cli._run_registries(args) == 1
    assert "error:" in capsys.readouterr().err


def test_list_prints_a_table(monkeypatch, capsys) -> None:
    def view(slug: str, enabled: bool, state: RegistryState | None) -> RegistryResponse:
        return RegistryResponse(slug=slug, name=slug, ecosystem="pypi", protocol="pypi-warehouse", base_url="https://x/",
                                priority=10, enabled=enabled, routing=None, config={}, auth_type="none",
                                credentials_ref=None, state=state, packages=3)

    degraded = RegistryState(status="failed", last_success_at=None, last_error="x", consecutive_failures=5,
                             degraded_until=None, degraded=True, cursor_data={})

    async def fake(db, ecosystem):
        assert ecosystem == "pypi"
        return [view("pypi", True, None), view("mirror", False, degraded)]

    monkeypatch.setattr(service, "list_registries", fake)
    assert cli._run_registries(cli.build_parser().parse_args(["registries", "list", "--ecosystem", "pypi"])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["SLUG", "ECOSYSTEM", "PROTOCOL", "PRIORITY", "ENABLED", "STATE", "PACKAGES"]
    assert lines[1].split() == ["pypi", "pypi", "pypi-warehouse", "10", "yes", "-", "3"]
    assert lines[2].split() == ["mirror", "pypi", "pypi-warehouse", "10", "no", "degraded", "3"]
