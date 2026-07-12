"""Structural (L1) classifier — port of package-classifire classify layer.

Facet model: roles (what the package is in a workflow) + capabilities
(machine-checkable facts). No КПО/ППО/СПО verdicts here: the pipeline records
facts; the "executable-only program?" decision belongs to a future LLM layer
(llm_verdicts is reserved for it).

Rules iteration = bump CLASSIFIER_VERSION + re-run classification (offline).
Version history is preserved: rows of older classifier versions stay.
"""

CLASSIFIER_VERSION = "2.4.0"

ROLES = (
    "runtime-component",
    "buildtime-component",
    "dev-config",
    "asset",
    "dev-tool",
    "dev-service",
    "service",
    "application",
    "platform-runtime",
    "binary-payload",
)

ROLE_LIFECYCLE = {
    "runtime-component": "use-time",
    "asset": "use-time",
    "service": "use-time",
    "application": "use-time",
    "platform-runtime": "use-time",
    "binary-payload": "use-time",
    "buildtime-component": "build-time",
    "dev-config": "build-time",
    "dev-service": "build-time",
    "dev-tool": "build-time",
}

CAPABILITIES = (
    "importable-api",
    "cli",
    "gui-app",
    "tui-app",
    "interactive-prompts",
    "dev-server",
    "server",
    "binary-payload",
    "platform-runtime",
    "plugin",
    "install-script",
    "sdist-only",
    "types-only",
    "config-only",
    "data-only",
    "deprecated",
)

STATUSES = ("ok", "needs-data", "undecidable-by-spec")
