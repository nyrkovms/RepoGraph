"""Initial contents of the rule registries (ported from classifire.registries).

The registries live in PostgreSQL so curators can edit them without code
changes; seeding is INSERT ... ON CONFLICT DO NOTHING — local edits survive.
Decisions referenced: classification-disputed-points.md of package-classifire
(entry-point group taxonomy PyPI-1/PyPI-6, curated overrides NPM-2, PyPI-3/4/5).
"""

# pattern, group_type, role_effect, note, host
ENTRY_GROUP_RULES = [
    # build/dev host plugins -> buildtime-component
    ("pytest11", "build-dev-plugin", "buildtime-component", "pytest plugin", "pytest"),
    ("pyinstaller40", "build-dev-plugin", "buildtime-component", "PyInstaller hook", "pyinstaller"),
    ("flake8.*", "build-dev-plugin", "buildtime-component", "flake8 plugin", "flake8"),
    ("sphinx.*", "build-dev-plugin", "buildtime-component", "Sphinx extension", "sphinx"),
    ("mkdocs.*", "build-dev-plugin", "buildtime-component", "MkDocs plugin", "mkdocs"),
    ("babel.*", "build-dev-plugin", "buildtime-component", "Babel extractor", "babel"),
    ("validate_pyproject.*", "build-dev-plugin", "buildtime-component", "pyproject schema plugin", "validate-pyproject"),
    ("distutils*", "build-dev-plugin", "buildtime-component", "distutils hook", "setuptools"),
    ("setuptools*", "build-dev-plugin", "buildtime-component", "setuptools hook", "setuptools"),
    ("virtualenv.*", "build-dev-plugin", "buildtime-component", "virtualenv plugin", "virtualenv"),
    ("egg_info.writers", "build-dev-plugin", "buildtime-component", "packaging metadata writer", "setuptools"),
    ("tox", "build-dev-plugin", "buildtime-component", "tox plugin", "tox"),
    ("hatch", "build-dev-plugin", "buildtime-component", "hatch plugin", "hatch"),
    ("poetry.*", "build-dev-plugin", "buildtime-component", "poetry plugin", "poetry"),
    ("pylint.*", "build-dev-plugin", "buildtime-component", "pylint plugin", "pylint"),
    ("bandit.*", "build-dev-plugin", "buildtime-component", "bandit plugin/extension", "bandit"),
    # runtime extension points -> runtime-component (не buildtime!)
    ("opentelemetry_*", "runtime-extension", "runtime-component", "OTel plugin discovery", "opentelemetry"),
    ("pandas_plotting_backends", "runtime-extension", "runtime-component", "pandas backend", "pandas"),
    ("networkx.backends", "runtime-extension", "runtime-component", "networkx backend", "networkx"),
    ("networkx.plugins", "runtime-extension", "runtime-component", "networkx backend (legacy)", "networkx"),
    ("sqlalchemy.dialects", "runtime-extension", "runtime-component", "SQLAlchemy dialect", "sqlalchemy"),
    ("keyring.backends", "runtime-extension", "runtime-component", "keyring backend", "keyring"),
    ("matplotlib.backend", "runtime-extension", "runtime-component", "matplotlib backend", "matplotlib"),
    ("fsspec.specs", "runtime-extension", "runtime-component", "fsspec filesystem", "fsspec"),
    ("array_api", "runtime-extension", "runtime-component", "array API integration", "numpy"),
    ("pygments.*", "runtime-extension", "runtime-component", "Pygments lexer/style", "pygments"),
    ("markdown.extensions", "runtime-extension", "runtime-component", "markdown extension", "markdown"),
    ("pkg_config", "runtime-extension", None, "build metadata exposure, роль не добавляет", None),
    # service/app integration -> сигнал сервиса, но слабый
    ("paste.server_runner", "service-integration", "service", "WSGI server runner", "paste"),
    ("celery.*", "service-integration", "service", "Celery integration", "celery"),
    ("mlflow.*", "service-integration", "service", "MLflow app/deployment", "mlflow"),
    # app-extension: собственная плагинная система приложения, роль не меняет
    ("certbot.*", "runtime-extension", None, "плагинная система certbot", "certbot"),
    # packaging/tool integration -> инструментальная интеграция, роль хоста
    ("pipx.run", "packaging-integration", None, "запускаемость через pipx; роль решает семантика", "pipx"),
    ("twine.registered_commands", "packaging-integration", "dev-tool", "twine command", "twine"),
    ("hatchling.*", "packaging-integration", "buildtime-component", "hatchling plugin", "hatchling"),
]

# ecosystem, name, roles, note
ROLE_OVERRIDES = [
    # npm: dual-use фреймворки и серверы (NPM-2)
    ("npm", "next", ["runtime-component", "dev-tool", "dev-service", "service"], "prod/dev serving modes"),
    ("npm", "nuxt", ["runtime-component", "dev-tool", "dev-service", "service"], "prod/dev serving modes"),
    ("npm", "astro", ["runtime-component", "dev-tool", "dev-service", "service"], "prod/dev serving modes"),
    ("npm", "vite", ["buildtime-component", "dev-tool", "dev-service"], "bundler + dev server"),
    ("npm", "tailwindcss", ["buildtime-component", "dev-tool"], "css build tool"),
    ("npm", "prisma", ["dev-tool", "dev-service"], "CLI + studio"),
    ("npm", "playwright", ["buildtime-component", "dev-tool"], "test framework + runner"),
    ("npm", "cypress", ["dev-tool", "dev-service"], "test runner + UI"),
    ("npm", "storybook", ["dev-tool", "dev-service"], "component workshop"),
    ("npm", "@storybook/react", ["buildtime-component", "dev-service"], "storybook renderer"),
    ("npm", "pm2", ["service", "dev-tool"], "решение из disputed-points: ядро — production process manager"),
    ("npm", "serve", ["service", "dev-service"], "static server, dual-use"),
    ("npm", "http-server", ["service", "dev-service"], "static server, dual-use"),
    ("npm", "bootstrap", ["asset", "runtime-component"], "css + js"),
    ("npm", "node", ["platform-runtime"], "решение NPM-1: runtime platform"),
    # pypi: решения из disputed-points
    ("pypi", "django", ["runtime-component", "dev-tool", "dev-service"], "framework + manage.py + runserver"),
    ("pypi", "flask", ["runtime-component", "dev-tool", "dev-service"], "framework + cli + dev server"),
    ("pypi", "celery", ["runtime-component", "service"], "library + worker process"),
    ("pypi", "dramatiq", ["runtime-component", "service"], "worker framework (PyPI-5)"),
    ("pypi", "rq", ["runtime-component", "service"], "worker framework (PyPI-5)"),
    ("pypi", "apscheduler", ["runtime-component"], "in-process scheduler (PyPI-5)"),
    ("pypi", "flower", ["service"], "ops monitoring UI = service (PyPI-5)"),
    ("pypi", "mlflow", ["runtime-component", "service", "dev-tool"], "library + tracking server + cli"),
    ("pypi", "scrapy", ["runtime-component", "dev-tool"], "framework + cli"),
    ("pypi", "sphinx", ["dev-tool", "buildtime-component"], "doc generator"),
    ("pypi", "mkdocs", ["dev-tool", "dev-service"], "doc generator + serve"),
    ("pypi", "setuptools", ["buildtime-component", "dev-tool", "runtime-component"], "build backend + pkg_resources"),
    ("pypi", "wheel", ["buildtime-component", "dev-tool"], "build component + cli"),
    ("pypi", "uvicorn", ["service", "dev-service"], "ASGI server, dual-use"),
    ("pypi", "jupyterlab", ["application", "dev-service"], "решение PyPI-3: default application"),
    ("pypi", "bpython", ["application"], "решение PyPI-3: интерактивный интерпретатор = application"),
    ("pypi", "watchfiles", ["runtime-component", "dev-service"], "решение PyPI-3: watcher = dev-service, не application"),
    ("pypi", "dvc", ["dev-tool", "runtime-component"], "cli + api"),
    # package/project managers без доступных entry points (PyPI-4)
    ("pypi", "uv", ["dev-tool"], "PyPI-4: package manager, wheel слишком большой для probe"),
    ("pypi", "poetry", ["dev-tool"], "PyPI-4: package manager"),
    ("pypi", "pdm", ["dev-tool"], "PyPI-4: package manager"),
    ("pypi", "pipx", ["dev-tool"], "PyPI-4: command runner/installer"),
    # самостоятельные приложения: bin/CLI есть, но L1-семантики в описании не хватает
    ("npm", "@google/gemini-cli", ["application"], "AI coding agent, интерактивный TUI"),
    ("npm", "@openai/codex", ["application"], "AI coding agent, интерактивный TUI"),
    ("npm", "@bitwarden/cli", ["application"], "менеджер паролей, end-user CLI"),
    ("npm", "fkill-cli", ["application"], "интерактивный убийца процессов"),
    ("npm", "gtop", ["application"], "системный монитор, TUI-дашборд"),
    ("npm", "carbonyl", ["application"], "браузер в терминале"),
    ("npm", "terminalizer", ["application"], "запись терминала в GIF, end-user утилита"),
    ("pypi", "mycli", ["application"], "интерактивный клиент MySQL (как pgcli/iredis)"),
    ("pypi", "ranger-fm", ["application"], "файловый менеджер TUI; sdist-only, cli структурно не виден"),
    ("pypi", "speedtest-cli", ["application"], "замер скорости сети, end-user утилита"),
    ("pypi", "magic-wormhole", ["application"], "передача файлов между машинами"),
    ("pypi", "borgbackup", ["application"], "backup-инструмент; sdist-only, cli структурно не виден"),
    ("pypi", "sqlmap", ["application"], "security testing утилита"),
    ("pypi", "certbot", ["application"], "получение TLS-сертификатов, ops-приложение с плагинами"),
]
