# Resolver reference cases (SPEC-MULTI-ECOSYSTEM.md §14.2)

Each case is a real project resolved by the **native tool** of its ecosystem; the resolver must produce the same set
of `(package, version)`. Cases live in `<ecosystem>/<case>/`:

- `case.yaml` — the request and the sources:

  ```yaml
  description: what the case covers
  roots: [{purl: pkg:pypi/torch, constraint: "==2.9.1", extras: []}]
  environment: {python_version: "3.12", python_full_version: "3.12.7", sys_platform: linux, os_name: posix,
                platform_machine: x86_64, platform_system: Linux}
  sources:                 # the profile, in order; `pypi` is the catalog's pypi.org source, others are created
    - slug: ref-pytorch-cpu
      protocol: pypi-simple
      base_url: https://download.pytorch.org/whl/cpu/
      routing: {mode: exclusive, packages: [torch]}
    - slug: pypi
  native:                  # how the generator calls the native tool (PyPI: pip in a python image)
    args: [--index-url, https://download.pytorch.org/whl/cpu, --extra-index-url, https://pypi.org/simple]
    platform: linux/amd64  # docker platform; pip runs natively there (markers follow the interpreter)
  allowed_differences:     # expected, explained mismatches; anything else fails the case
    - {package: example, reason: "why the native tool and we legitimately differ"}
  ```

- `expected.json` — written by `scripts/generate_resolver_references.sh`, never by hand:
  `{"tool": "pip 24.3.1", "command": [...], "generated_at": "...", "install": [{"name": ..., "version": ...}]}`.

`tests/integration/test_resolver_reference.py` resolves every case through the API service on recorded upstream
responses (`HTTP_REPLAY_DIR`, no network) and compares the sets; a case without `expected.json` is skipped. The
metric is the share of matching `(package, version)` pairs, target 100%.

Generating: `scripts/generate_resolver_references.sh [case-dir ...]` (live network, docker). Re-record the upstream
responses the cases need with `HTTP_RECORD_DIR` when a case is added or its expectation changes.
