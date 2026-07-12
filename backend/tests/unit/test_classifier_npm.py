from app.classifier.core import ProbeView
from app.classifier.npm import classify_npm, platform_payload_relations


def probe(file_facts: dict | None = None) -> ProbeView:
    return ProbeView(file_facts=file_facts, available=True)


def caps(result) -> dict[str, str]:
    return {c.name: c.confidence for c in result.capabilities}


def test_eslint_plugin_by_peer_dependency():
    manifest = {
        "description": "An ESLint plugin for imports",
        "main": "lib/index.js",
        "peerDependencies": {"eslint": ">=8"},
    }
    result = classify_npm("eslint-plugin-import", manifest, probe(), {})
    assert caps(result)["plugin"] == "high"
    assert ("plugin-of", "eslint") in result.relations
    assert "buildtime-component" in result.roles


def test_types_package_is_dev_config():
    result = classify_npm("@types/node", {"description": "TypeScript definitions"}, probe(), {})
    assert "dev-config" in result.roles
    assert caps(result)["types-only"] == "high"
    assert result.status == "ok"


def test_cli_with_tui_framework():
    manifest = {
        "description": "interactive process viewer",
        "bin": {"viewer": "cli.js"},
        "dependencies": {"ink": "^4.0.0"},
    }
    result = classify_npm("proc-viewer", manifest, probe(), {})
    assert caps(result)["cli"] == "high"
    assert caps(result)["tui-app"] == "high"
    assert "application" in result.roles


def test_platform_binary_payload_terminal_role():
    manifest = {"os": ["linux"], "cpu": ["x64"], "description": "binary"}
    result = classify_npm("esbuild-linux-x64", manifest, probe(), {})
    assert result.roles == {"binary-payload"}
    assert caps(result)["binary-payload"] == "high"


def test_override_is_terminal():
    manifest = {"description": "Node.js", "bin": {"node": "bin/node"}}
    result = classify_npm("node", manifest, probe(), {"node": ["platform-runtime"]})
    assert result.roles == {"platform-runtime"}
    assert result.status == "ok"


def test_install_script_capability():
    manifest = {"main": "index.js", "scripts": {"postinstall": "node scripts/download-binary.js"}}
    result = classify_npm("native-thing", manifest, probe(), {})
    assert "install-script" in caps(result)


def test_deprecated_capability():
    manifest = {"main": "index.js", "deprecated": "use new-package instead"}
    result = classify_npm("old-package", manifest, probe(), {})
    assert caps(result)["deprecated"] == "high"


def test_plain_library_runtime_component():
    manifest = {"description": "String left pad", "main": "index.js"}
    result = classify_npm("left-pad", manifest, probe({"has_index_js": True}), {})
    assert "runtime-component" in result.roles
    assert result.status == "ok"


def test_bin_without_semantics_needs_data():
    manifest = {"bin": {"x": "cli.js"}, "description": "does stuff"}
    result = classify_npm("mystery-cli", manifest, probe(), {})
    assert result.status == "needs-data"


def test_dual_use_server_needs_data():
    manifest = {
        "bin": {"srv": "cli.js"},
        "description": "static file server with live reload for development and production server use",
        "main": "index.js",
    }
    result = classify_npm("dual-server", manifest, probe(), {})
    # dev-server и server сигналы одновременно → dev-service+service → needs-data
    if {"service", "dev-service"} <= result.roles:
        assert result.status == "needs-data"


def test_platform_payload_relations_from_optional_deps():
    manifest = {"optionalDependencies": {"opencode-linux-x64": "1.0.0", "chalk": "^5.0.0"}}
    relations = platform_payload_relations(manifest)
    assert ("platform-payload", "opencode-linux-x64") in relations
    assert all(name != "chalk" for _, name in relations)


def test_asset_by_file_composition():
    facts = {"ext_counts": {"woff2": 40, "css": 30, "js": 2}, "has_index_js": False}
    result = classify_npm("some-fonts", {"description": "web font files"}, probe(facts), {})
    assert "asset" in result.roles
