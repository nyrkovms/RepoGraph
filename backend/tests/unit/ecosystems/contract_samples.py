"""Reference data for the plugin contract suite (SPEC-MULTI-ECOSYSTEM.md §14.1).

Every plugin adds an entry when it is registered; the suite fails for a registered plugin without samples.
`ordered_versions` must be strictly increasing in the ecosystem's own order (checked against the native
implementation when the entry is written, see design/schema-check).
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Samples:
    # (native coordinates, purl without version)
    names: list[tuple[str, str]]
    ordered_versions: list[str]
    invalid_versions: list[str] = field(default_factory=list)
    prereleases: list[str] = field(default_factory=list)


SAMPLES: dict[str, Samples] = {
    "pypi": Samples(
        names=[("Flask", "pkg:pypi/flask"), ("typing_extensions", "pkg:pypi/typing-extensions"), ("Jinja2", "pkg:pypi/jinja2")],
        # Checked against packaging.version.Version.
        ordered_versions=["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1", "2.5.1", "2.5.1+cpu", "2.5.1+cu121", "1!0.1"],
        invalid_versions=["not-a-version", "1.0-foo-bar"],
        prereleases=["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1"],
    ),
    "npm": Samples(
        names=[("left-pad", "pkg:npm/left-pad"), ("@angular/core", "pkg:npm/%40angular/core"), ("react", "pkg:npm/react")],
        # Checked against node-semver 7.6.3 (tests/fixtures/npm/semver/expected.json).
        ordered_versions=["0.0.0-20191109021931-daa7c04131f5", "0.1.0", "1.0.0-0", "1.0.0-alpha", "1.0.0-alpha.1",
                          "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.9.0", "1.10.0", "2.0.0", "10.0.0"],
        invalid_versions=["1.2", "01.2.3", "9007199254740992.0.0", "latest"],
        prereleases=["0.0.0-20191109021931-daa7c04131f5", "1.0.0-0", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta.2",
                     "1.0.0-beta.11", "1.0.0-rc.1"],
    ),
}
