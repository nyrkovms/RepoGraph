"""pypi structural classifier (ported from classifire.classify.classify_pypi).

Input: manifest {"info": ..., "urls": ...} (packages.raw_metadata for
pypi-json), the representative probe view and the entry-group rule registry.
Logic is kept 1:1 with classifire v2.4.0.
"""

import re
from typing import Any

from app.classifier.core import ProbeView, Result, has_any, lower_blob, match_entry_group, parse_entry_points

PYPI_DEV_TOOL_NAMES = {
    "pytest", "tox", "nox", "coverage", "black", "ruff", "flake8", "mypy", "isort",
    "bandit", "pylint", "pip", "uv", "poetry", "hatch", "pdm", "twine", "build",
    "pre-commit", "cython", "cookiecutter", "maturin", "pyinstaller", "nuitka",
    "cx-freeze", "pipx", "pip-tools", "semgrep", "safety", "pip-audit",
}
PYPI_SERVICE_NAMES = {"gunicorn", "uvicorn", "waitress", "daphne", "hypercorn", "granian"}
PYPI_APPLICATION_NAMES = {"httpie", "yt-dlp", "awscli", "ansible", "glances", "jupyterlab"}
PYPI_ASSET_NAMES = {"certifi", "tzdata", "nltk-data"}
PYPI_MANAGER_PATTERNS = ["package manager", "project manager", "dependency management",
                         "build frontend", "installer", "virtual environment"]

PYPI_TUI_DEPS = {"textual", "urwid", "py-cui", "blessed", "asciimatics", "npyscreen", "pytermgui"}
PYPI_PROMPT_DEPS = {"prompt-toolkit", "prompt_toolkit", "questionary", "inquirerpy"}


def classify_pypi(name: str, manifest: dict[str, Any], probe: ProbeView | None,
                  overrides: dict[str, list[str]], entry_rules: list[dict]) -> Result:
    result = Result()
    info = manifest.get("info") or {}
    summary = str(info.get("summary") or "")
    result.summary = summary
    classifiers = info.get("classifiers") or []
    requires_dist = info.get("requires_dist") or []
    keywords = info.get("keywords") or ""
    text = lower_blob(name, summary, keywords, classifiers, requires_dist)
    lname = name.lower()

    entry_points = parse_entry_points(probe.entry_points_txt if probe else None)
    probe_failed = bool(probe and probe.error) or probe is None or not probe.available
    console_scripts = entry_points.get("console_scripts", [])
    gui_scripts = entry_points.get("gui_scripts", [])
    top_level = (
        [line.strip() for line in (probe.top_level_txt or "").splitlines() if line.strip()] if probe else []
    )
    file_facts = (probe.file_facts if probe else None) or {}
    data_scripts = file_facts.get("scripts") or []  # bin-скрипты мимо console_scripts (так ставит ruff)
    py_files = int((file_facts.get("ext_counts") or {}).get("py", 0))
    platform_wheel = bool(file_facts.get("platform_wheel"))
    is_stub = lname.startswith("types-") or lname.endswith("-stubs") or "typing :: stubs" in text
    has_import = bool(top_level) or not is_stub
    has_cli = bool(console_scripts or data_scripts)

    # --- capabilities ---
    if console_scripts:
        result.add_capability("cli", "high", "console_scripts: " + ", ".join(console_scripts[:5]))
    elif data_scripts:
        result.add_capability("cli", "high", "wheel data scripts: " + ", ".join(data_scripts[:5]))
    if gui_scripts:
        result.add_capability("gui-app", "high", "gui_scripts: " + ", ".join(gui_scripts[:5]))
    if top_level:
        result.add_capability("importable-api", "high", "top_level modules: " + ", ".join(top_level[:5]))
    elif py_files >= 4:
        result.add_capability("importable-api", "low", f"{py_files} python files in wheel, no top_level.txt")
    elif file_facts and py_files <= 3 and platform_wheel and data_scripts:
        # платформенный wheel с бинарником и py-обёрткой (ruff, maturin) — не библиотека
        pass
    elif py_files >= 1:
        result.add_capability("importable-api", "low", f"{py_files} python files in wheel")
    elif has_import and (probe_failed or not file_facts):
        result.add_capability("importable-api", "low", "no wheel facts; import assumed for python distribution")
    if platform_wheel and data_scripts and py_files <= 3:
        result.add_capability("binary-payload", "high",
                              "platform wheel ships executable binary with thin python shim")
    if is_stub:
        result.add_capability("types-only", "high", "type stub naming/classifier")
    urls = manifest.get("urls") or []
    if urls and not any(u.get("packagetype") == "bdist_wheel" for u in urls):
        result.add_capability("sdist-only", "high", "no wheel published; install executes sdist build")
    if urls and all(u.get("yanked") for u in urls):
        reasons = {str(u.get("yanked_reason") or "") for u in urls} - {""}
        result.add_capability("deprecated", "high",
                              "all release files yanked" + (": " + "; ".join(sorted(reasons))[:150] if reasons else ""))
    elif "development status :: 7 - inactive" in text:
        result.add_capability("deprecated", "low", "classifier Development Status :: 7 - Inactive")
    if lname in PYPI_ASSET_NAMES:
        result.add_capability("data-only", "high", "known data payload package")
    req_names = {re.split(r"[ ;(<>=!~\[]", str(r))[0].lower() for r in requires_dist}
    tui_deps = req_names & PYPI_TUI_DEPS
    prompt_deps = req_names & PYPI_PROMPT_DEPS
    if has_cli and tui_deps:
        result.add_capability("tui-app", "high", "TUI framework dependency: " + ", ".join(sorted(tui_deps)))
    elif has_cli and has_any(text, ["terminal ui", "curses", "full-screen", "fullscreen"]):
        result.add_capability("tui-app", "low", "terminal UI keywords alongside console script")
    if has_cli and prompt_deps:
        result.add_capability("interactive-prompts", "high",
                              "prompt-library dependency: " + ", ".join(sorted(prompt_deps)[:3]))
    elif has_cli and "interactive" in text and not tui_deps:
        result.add_capability("interactive-prompts", "low", "'interactive' keyword alongside console script")
    if has_any(text, ["wsgi :: server", "asgi :: server", "application server", "production server"]):
        result.add_capability("server", "high", "WSGI/ASGI/production server semantics")
    if has_cli and has_any(text, ["auto-reload", "autoreload", "live reload", "development server",
                                          "serve documentation", "watch"]):
        result.add_capability("dev-server", "high", "dev serve/watch semantics alongside console script")

    # --- роли ---
    if lname in overrides:
        for role in overrides[lname]:
            result.add_facet(role, "high", "curated override")
        # кураторский набор ролей терминален: эвристики роли не доклеивают
        return result

    if is_stub:
        result.add_facet("dev-config", "high", "type stub naming/classifier signal")

    if lname in PYPI_ASSET_NAMES or has_any(text, ["timezone data", "ca bundle", "certificate bundle"]):
        result.add_facet("asset", "high", "runtime data/payload signal")

    # entry-point groups через реестр (PyPI-1)
    unknown_groups: list[str] = []
    plugin_hosts: dict[str, str] = {}
    for group in entry_points:
        if group in ("console_scripts", "gui_scripts"):
            continue
        rule = match_entry_group(entry_rules, group)
        if rule is not None and rule["host"] and rule["host"] != lname:
            plugin_hosts[rule["host"]] = group
        if rule is None:
            unknown_groups.append(group)
        elif rule["role_effect"]:
            confidence = "low" if rule["group_type"] == "service-integration" else "high"
            result.add_facet(rule["role_effect"], confidence,
                             f"entry-point group [{group}] -> {rule['group_type']}")

    if plugin_hosts:
        result.add_capability("plugin", "high",
                              "entry-point group(s): " + ", ".join(f"[{g}] -> {h}" for h, g in sorted(plugin_hosts.items())))
        for host in sorted(plugin_hosts):
            result.relations.append(("plugin-of", host))

    if gui_scripts:
        result.add_facet("application", "high", "non-empty gui_scripts entry point")

    if has_any(text, ["framework :: pytest", "framework :: flake8", "sphinx :: extension", "mkdocs",
                      "pytest plugin", "flake8 extension"]):
        result.add_facet("buildtime-component", "high", "host-plugin classifier/summary")
    if lname in {"setuptools", "hatchling", "flit-core", "poetry-core"}:
        result.add_facet("buildtime-component", "high", "PEP 517 build backend")

    # dev-tool (включая PyPI-4: package managers без probe)
    if lname in PYPI_DEV_TOOL_NAMES or (
        has_cli
        and has_any(text, ["software development :: build tools", "software development :: testing",
                           "software development :: quality assurance", "compiler", "code generator",
                           "documentation generator", "formatter", "formatting", "lint", "type check",
                           "test runner", "build frontend", "package installer"])
    ) or (probe_failed and has_any(text, PYPI_MANAGER_PATTERNS)):
        result.add_facet("dev-tool", "high" if (has_cli or lname in PYPI_DEV_TOOL_NAMES) else "low",
                         "build/test/QA/packaging CLI semantics")

    # service
    if lname in PYPI_SERVICE_NAMES or has_any(text, ["wsgi :: server", "asgi :: server"]) or (
        has_cli and has_any(text, ["application server", "web server gateway interface", "production server"])
    ):
        result.add_facet("service", "high", "WSGI/ASGI/server semantics")

    # dev-service
    if has_cli and has_any(text, ["auto-reload", "autoreload", "live reload", "development server",
                                          "serve documentation", "watch"]):
        result.add_facet("dev-service", "high", "developer-facing server/watch process")

    # application
    sysadmin_signal = "intended audience :: system administrators" in text and "topic :: system" in text
    if lname in PYPI_APPLICATION_NAMES or (
        has_cli
        and (sysadmin_signal or has_any(text, ["command line client", "command-line client", "terminal ui",
                                               "curses", "end users/desktop", "monitoring",
                                               "automation platform"]))
        and not (result.roles & {"dev-tool", "service"})
    ):
        result.add_facet("application", "high", "positive end-user/ops CLI signal")

    # runtime-component: build hook не стирает runtime-фасет (PyPI-2)
    process_roles = {"dev-tool", "service", "application", "dev-service"}
    has_process_role = bool(result.roles & process_roles)
    buildtime_only = "buildtime-component" in result.roles and not has_process_role
    runtime_library_signal = has_any(text, ["software development :: libraries", "client library", "library",
                                            "framework", "template engine", "interface", "sdk", "package for"])
    plugin_only = bool(re.match(r"^(pytest|flake8|sphinx|mkdocs)-", lname)) or has_any(
        text, ["plugin for pytest", "pytest plugin", "flake8 plugin", "sphinx extension"]
    )
    if not result.facets:
        result.add_facet("runtime-component", "high", "default importable Python distribution")
    elif buildtime_only and runtime_library_signal and not plugin_only:
        result.add_facet("runtime-component", "high", "library/framework signal alongside build-time hook (PyPI-2)")
    elif has_process_role and top_level and "runtime-component" not in result.roles and not plugin_only:
        result.add_facet("runtime-component", "low", "importable top-level modules alongside executable facet")

    # --- статус ---
    roles = result.roles
    if "application" in roles and "dev-service" in roles and lname not in overrides:
        result.status = "undecidable-by-spec"
        result.status_reason = "application vs dev-service boundary is explicitly open in methodology"
        for facet in result.facets:
            if facet.name in {"application", "dev-service"}:
                facet.provisional = True
    elif "application" in roles and "dev-tool" in roles and lname not in overrides:
        result.status = "undecidable-by-spec"
        result.status_reason = "application vs dev-tool boundary needs tie-break"
        for facet in result.facets:
            if facet.name in {"application", "dev-tool"}:
                facet.provisional = True
    elif {"service", "dev-service"} <= roles and lname not in overrides:
        result.status = "needs-data"
        result.status_reason = "same package has prod and dev serving modes; mode-level review needed"
    elif unknown_groups:
        result.status = "needs-data"
        result.status_reason = "unknown entry-point group(s): " + ",".join(sorted(unknown_groups)[:5])
    elif probe_failed and not result.facets:
        result.status = "needs-data"
        result.status_reason = "wheel probe unavailable and no semantic signal"
    return result
