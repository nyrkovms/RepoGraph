"""npm structural classifier (ported from classifire.classify.classify_npm).

Input: the version manifest (package_versions.raw_metadata for npm) and the
representative probe view. Logic is kept 1:1 with classifire v2.4.0 so the
dataset stays comparable; rules iteration = bump CLASSIFIER_VERSION.
"""

import re
from typing import Any

from app.classifier.core import PLATFORM_SUFFIX, ProbeView, Result, has_any, lower_blob

NPM_ASSET_NAMES = {
    "normalize.css", "modern-normalize", "@fortawesome/fontawesome-free",
    "material-icons", "material-design-icons", "typeface-roboto",
}
NPM_DEV_TOOL_NAMES = {
    "eslint", "prettier", "stylelint", "jest", "vitest", "mocha", "ava", "webpack",
    "rollup", "parcel", "esbuild", "typescript", "ts-node", "tsx", "tsup", "swc",
    "npm", "yarn", "pnpm", "lerna", "nx", "turbo", "semantic-release", "typedoc",
    "jsdoc", "openapi-typescript", "firebase-tools", "aws-cdk", "serverless",
}
NPM_SERVICE_NAMES = {"pm2", "verdaccio", "http-server", "serve"}
NPM_APPLICATION_NAMES = {"tldr", "npkill", "@anthropic-ai/claude-code", "@qwen-code/qwen-code", "opencode-ai"}

# Хосты-инструменты: peerDependency на них = «я исполняюсь внутри хоста» (плагин).
# Peer на библиотеку/фреймворк (react, vue) плагином НЕ считается — это совместимость.
NPM_PLUGIN_HOSTS = {"eslint", "webpack", "rollup", "vite", "postcss", "stylelint", "prettier",
                    "grunt", "gulp", "@babel/core", "jest", "vitest", "tailwindcss", "storybook"}
# Конвенции имён: паттерн -> хост (слабее peer, но общеприняты).
NPM_PLUGIN_NAME_PATTERNS = [
    (re.compile(r"(^|/)eslint-(plugin|config)-"), "eslint"),
    (re.compile(r"(^|/)(@rollup/plugin-|rollup-plugin-)"), "rollup"),
    (re.compile(r"(^|/)vite-plugin-"), "vite"),
    (re.compile(r"(^|/)(babel-(plugin|preset)-|@babel/(plugin|preset)-)"), "@babel/core"),
    (re.compile(r"-loader$|-webpack-plugin$"), "webpack"),
    (re.compile(r"(^|/)stylelint-(plugin|config)-"), "stylelint"),
    (re.compile(r"(^|/)grunt-"), "grunt"),
    (re.compile(r"(^|/)gulp-"), "gulp"),
    (re.compile(r"(^|/)postcss-"), "postcss"),
]

# Полноэкранные TUI-фреймворки — сильный сигнал; интерактивные prompt-библиотеки — слабый.
NPM_TUI_DEPS = {"ink", "blessed", "neo-blessed", "blessed-contrib", "terminal-kit", "vorpal"}
NPM_PROMPT_DEPS = {"inquirer", "prompts", "enquirer"}

ASSET_EXTS = {"css", "svg", "woff", "woff2", "ttf", "otf", "eot", "png", "jpg", "gif", "ico", "json"}
CODE_EXTS = {"js", "cjs", "mjs", "ts", "cts", "mts", "jsx", "tsx", "node", "wasm"}


def classify_npm(name: str, manifest: dict[str, Any], probe: ProbeView | None,
                 overrides: dict[str, list[str]]) -> Result:
    result = Result()
    description = str(manifest.get("description") or "")
    result.summary = description
    keywords = manifest.get("keywords") or []
    deps: dict[str, str] = {}
    for key in ("dependencies", "peerDependencies", "optionalDependencies"):
        deps.update(manifest.get(key) or {})
    text = lower_blob(name, description, keywords, deps)
    lname = name.lower()

    bin_field = manifest.get("bin")
    has_bin = bool(bin_field)
    bin_names = sorted(bin_field.keys()) if isinstance(bin_field, dict) else ([name] if bin_field else [])
    declared_import = any(manifest.get(key) for key in ("main", "module", "exports", "browser"))
    file_facts = (probe.file_facts if probe else None) or {}
    implicit_index = bool(file_facts.get("has_index_js"))
    has_import = declared_import or (implicit_index and not has_bin)
    is_platform_binary = bool(manifest.get("os")) and bool(manifest.get("cpu")) or bool(PLATFORM_SUFFIX.search(lname))

    # --- capabilities (структурные факты) ---
    if has_bin:
        result.add_capability("cli", "high", "bin: " + ", ".join(bin_names[:5]))
    if declared_import:
        result.add_capability("importable-api", "high", "declared main/module/exports entry")
    elif implicit_index and not is_platform_binary:
        result.add_capability("importable-api", "low", "implicit index.js in tarball")
    if is_platform_binary and not declared_import:
        result.add_capability("binary-payload", "high", "os/cpu-restricted package or platform name suffix, no import entry")
    dep_names = {d.lower() for d in deps}
    tui_deps = dep_names & NPM_TUI_DEPS
    prompt_deps = (dep_names & NPM_PROMPT_DEPS) | {d for d in dep_names if d.startswith("@inquirer/")}
    if has_bin and tui_deps:
        result.add_capability("tui-app", "high", "TUI framework dependency: " + ", ".join(sorted(tui_deps)))
    elif has_bin and has_any(text, ["terminal ui", "tui", "curses", "full-screen", "fullscreen"]):
        result.add_capability("tui-app", "low", "terminal UI keywords alongside bin")
    if has_bin and prompt_deps:
        result.add_capability("interactive-prompts", "high",
                              "prompt-library dependency: " + ", ".join(sorted(prompt_deps)[:3]))
    elif has_bin and "interactive" in text and not tui_deps:
        result.add_capability("interactive-prompts", "low", "'interactive' keyword alongside bin")
    if has_bin and has_any(text, ["dev server", "development server", "hot reload", "live reload", "watch mode"]):
        result.add_capability("dev-server", "high", "dev-server semantics alongside bin")
    if has_bin and has_any(text, ["production server", "web server", "http server", "static file server",
                                  "process manager", "proxy server", "daemon"]):
        result.add_capability("server", "high", "server/daemon semantics alongside bin")
    if lname == "node" or (is_platform_binary and lname.startswith("node-")):
        result.add_capability("platform-runtime", "high", "Node.js runtime distribution")
    if lname.startswith("@types/") or re.search(r"(-|/)types$|-interop-types$", lname):
        if not has_bin and not declared_import:
            result.add_capability("types-only", "high", "type declaration package without runtime entry")
    peer_deps = {d.lower() for d in (manifest.get("peerDependencies") or {})}
    peer_hosts = peer_deps & NPM_PLUGIN_HOSTS
    name_hosts = {host for pattern, host in NPM_PLUGIN_NAME_PATTERNS if pattern.search(lname)}
    if peer_hosts:
        result.add_capability("plugin", "high", "peerDependency on host tool: " + ", ".join(sorted(peer_hosts)))
    elif name_hosts:
        result.add_capability("plugin", "low", "plugin naming convention for: " + ", ".join(sorted(name_hosts)))
    for host in sorted(peer_hosts | name_hosts):
        if host != lname:
            result.relations.append(("plugin-of", host))
    if manifest.get("deprecated"):
        result.add_capability("deprecated", "high",
                              "registry deprecation: " + str(manifest.get("deprecated"))[:200])
    scripts = manifest.get("scripts") or {}
    install_hooks = [k for k in ("preinstall", "install", "postinstall") if scripts.get(k)]
    if install_hooks:
        result.add_capability("install-script", "high",
                              "; ".join(f"{k}: {str(scripts[k])[:60]}" for k in install_hooks))

    # --- роли ---
    if lname in overrides:
        for role in overrides[lname]:
            result.add_facet(role, "high", "curated override")

    is_override = lname in overrides
    if add_platform_roles_npm(result, lname, is_platform_binary, declared_import, has_bin):
        return finalize_npm(result, has_bin, has_import, is_override)
    if is_override:
        # кураторский набор ролей терминален: эвристики роли не доклеивают
        return finalize_npm(result, has_bin, has_import, True)

    # dev-config: типы и конфиги (NPM-4: шире, чем @types/*)
    if lname.startswith("@types/") or lname.startswith("@tsconfig/"):
        result.add_facet("dev-config", "high", "canonical type/tsconfig namespace")
        result.add_capability("types-only", "high", "canonical @types/@tsconfig namespace")
    elif re.search(r"(-|/)types$|-interop-types$", lname) and not has_bin and not declared_import:
        result.add_facet("dev-config", "high", "NPM-4 rule: types package without runtime entry")
    if re.search(r"(^|/)(eslint|stylelint|prettier)-config-|(^|/)tsconfig|(^|/)babel-preset-|(^|/)@babel/preset-", lname):
        result.add_facet("dev-config", "high", "config/preset naming convention")
        result.add_capability("config-only", "low", "config/preset naming convention")

    # asset: имя + состав файлов из probe (NPM-5)
    exts = file_facts.get("ext_counts") or {}
    asset_files = sum(count for ext, count in exts.items() if ext in ASSET_EXTS)
    code_files = sum(count for ext, count in exts.items() if ext in CODE_EXTS)
    looks_asset_by_name = lname in NPM_ASSET_NAMES or bool(re.search(r"\.css$|(font|icon|icons|emoji|asset)s?$", lname))
    not_asset_semantics = has_any(text, ["loader", "postcss", "eslint", "plugin", "compiler", "bundler",
                                         "parser", "minifier"])
    if not not_asset_semantics:
        if looks_asset_by_name and (not exts or asset_files >= code_files):
            result.add_facet("asset", "high", "static CSS/font/icon payload signal")
            result.add_capability("data-only", "low", "asset-dominant package")
        elif exts and asset_files > 3 * max(code_files, 1) and not has_bin and asset_files > 10:
            result.add_facet("asset", "high", f"file composition: {asset_files} asset vs {code_files} code files")
            result.add_capability("data-only", "high", f"{asset_files} asset files vs {code_files} code files")

    # buildtime-component
    if re.search(
        r"(^|/)(babel|css|style|postcss)-loader$|(^|/)@babel/(core|plugin-)|(^|/)postcss$"
        r"|(^|/)eslint-plugin-|(^|/)@rollup/plugin-|(^|/)rollup-plugin-|(^|/)vite-plugin-"
        r"|(^|/)babel-plugin-|(^|/)jest-|(^|/)@testing-library/",
        lname,
    ) or has_any(text, ["babel plugin", "babel preset", "webpack loader", "rollup plugin", "vite plugin",
                        "eslint plugin", "stylelint plugin", "postcss plugin", "jest environment",
                        "jest preset", "testing library", "mock service worker", "source map",
                        "abstract syntax tree", "syntax tree", "gruntplugin", "grunt plugin"]):
        result.add_facet("buildtime-component", "high", "plugin/loader/test/build component signal")
    if lname in {"msw", "@faker-js/faker"}:
        result.add_facet("buildtime-component", "high", "test/mock fixture package anchor")

    # dev-tool
    if lname in NPM_DEV_TOOL_NAMES or (
        has_bin
        and has_any(text, ["compiler", "transpiler", "bundler", "build tool", "build system", "linter",
                           "formatter", "test runner", "testing framework", "coverage", "scaffold",
                           "code generator", "schema generator", "json schema", "documentation generator",
                           "package manager", "monorepo", "release", "migration"])
    ):
        result.add_facet("dev-tool", "high" if has_bin else "low", "build/test/release CLI semantics")

    # dev-service
    if has_bin and has_any(text, ["dev server", "development server", "hot reload", "hot module",
                                  "live reload", "reload", "updates the browser on changes", "watch mode",
                                  "monitor script", "restart", "storybook", "mock server",
                                  "for testing", "for developers"]):
        result.add_facet("dev-service", "high", "developer-facing long-running process signal")

    # service
    dev_context = has_any(text, ["development", "developer", "for developers", "mock", "testing", "test ",
                                 "code generator", "schema generator", "documentation", "live reload",
                                 "hot reload"])
    if lname in NPM_SERVICE_NAMES or (
        has_bin and not dev_context
        and has_any(text, ["production server", "web server", "http server", "static file server",
                           "process manager", "private npm registry", "proxy server"])
    ):
        result.add_facet("service", "high", "server/process manager package signal")

    # application
    if lname in NPM_APPLICATION_NAMES or (
        has_bin
        and has_any(text, ["interactive", "terminal ui", "tui", "chat", "viewer", "download videos", "personal"])
        and not (result.roles & {"dev-tool", "dev-service", "service"})
    ):
        result.add_facet("application", "high", "positive end-user CLI/application signal")

    # runtime-component
    if has_import and result.roles & {"dev-tool", "service", "application", "dev-service"}:
        result.add_facet("runtime-component", "low", "importable API exists alongside executable facet")
    elif has_import and not (result.roles & {"dev-config", "asset", "buildtime-component"}):
        result.add_facet("runtime-component", "high", "declared importable runtime entry")

    return finalize_npm(result, has_bin, has_import, is_override)


def add_platform_roles_npm(result: Result, lname: str, is_platform_binary: bool,
                           declared_import: bool, has_bin: bool) -> bool:
    """platform-runtime / binary-payload: терминальные структурные роли."""
    if lname == "node" or (is_platform_binary and lname.startswith("node-")):
        result.add_facet("platform-runtime", "high", "Node.js runtime/platform distribution (NPM-1 decision)")
        return True
    if is_platform_binary and not declared_import and not has_bin:
        result.add_facet("binary-payload", "high", "platform-specific binary artifact without entries")
        return True
    return False


def finalize_npm(result: Result, has_bin: bool, has_import: bool, is_override: bool = False) -> Result:
    roles = result.roles
    if not roles:
        if has_bin:
            result.status = "needs-data"
            result.status_reason = "has bin but no positive semantic role signal"
        elif has_import:
            result.add_facet("runtime-component", "low", "importable entry with weak semantic signals")
        else:
            result.status = "needs-data"
            result.status_reason = "no bin/import/config/asset signal in package metadata"
        return result
    if is_override:
        return result  # роли утверждены кураторски — спорные комбинации уже разрешены
    if {"service", "dev-service"} <= roles:
        result.status = "needs-data"
        result.status_reason = "dual-use server package; production vs development serving requires review"
    elif "application" in roles and "dev-tool" in roles:
        result.status = "undecidable-by-spec"
        result.status_reason = "application vs dev-tool boundary needs explicit semantic decision"
        for facet in result.facets:
            if facet.name in {"application", "dev-tool"}:
                facet.provisional = True
    elif has_bin and has_import and roles == {"runtime-component"}:
        result.status = "needs-data"
        result.status_reason = "bin plus importable API without enough semantic evidence (L2/LLM candidate)"
    return result


def platform_payload_relations(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """optionalDependencies вида opencode-linux-x64 -> связь platform-payload."""
    relations: list[tuple[str, str]] = []
    for dep in (manifest.get("optionalDependencies") or {}):
        if PLATFORM_SUFFIX.search(dep.lower()):
            relations.append(("platform-payload", dep))
    return relations
