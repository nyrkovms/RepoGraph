"""#12: in a fresh process the built-in plugins must be there for the first caller, whatever it is."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def _fresh(code: str) -> str:
    """Run `code` in a new interpreter; its last line of output (the app logs to stdout too)."""
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "LOG_TO_FILE": "false"})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_resolve_finds_the_pypi_plugin_in_a_fresh_api_process() -> None:
    # The api right after a restart: the app is imported, no request has loaded the plugins yet.
    assert _fresh("import app.main\nfrom app.resolver.service import _plugin\nprint(_plugin('pypi').ecosystem)") == "pypi"


def test_get_plugin_loads_the_built_in_plugins() -> None:
    assert _fresh("from app.ecosystems.registry import get_plugin\nprint(get_plugin('pypi').ecosystem)") == "pypi"
