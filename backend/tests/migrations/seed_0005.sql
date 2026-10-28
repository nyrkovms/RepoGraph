-- Synthetic catalog at revision 20261027_0005 (IMPL P0.7). Covers what migration 0006 must transform:
-- NULL namespace, non-normalized dependency names, every dependencies_hydrated variant, requires_python,
-- last_serial, package deprecation, yanked and invalid versions, artifacts with PEP 658 flag,
-- vulnerabilities, authors, dist tags, update runs, sync failures and events.
BEGIN;

-- Migration 0001 seeds the pypi registry with a random id; give it a fixed one (nothing references it yet).
UPDATE registries SET id = md5('reg:pypi')::uuid WHERE slug = 'pypi';

INSERT INTO sync_states (id, registry_id, sync_type, status, cursor_data, packages_synced, packages_total)
VALUES (md5('sync:pypi')::uuid, md5('reg:pypi')::uuid, 'full', 'idle', '{}', 3, 3);

INSERT INTO packages (id, ecosystem, namespace, name, normalized_name, primary_registry_id, latest_version,
                      updated_at, synced_at, deprecated, deprecation_message, raw_metadata, last_serial)
VALUES
  (md5('pkg:flask')::uuid, 'pypi', NULL, 'Flask', 'flask', md5('reg:pypi')::uuid, '3.1.0', now(), now(), false, NULL, '{"last_serial": 100}', 100),
  (md5('pkg:old')::uuid, 'pypi', NULL, 'old_pkg', 'old-pkg', md5('reg:pypi')::uuid, 'not-a-version', now(), now(), true, 'use new-pkg', '{}', NULL),
  (md5('pkg:jinja2')::uuid, 'pypi', NULL, 'Jinja2', 'jinja2', md5('reg:pypi')::uuid, '3.1.4', now(), now(), false, NULL, '{}', 50);

INSERT INTO packages (id, ecosystem, namespace, name, normalized_name, primary_registry_id, latest_version,
                      updated_at, synced_at, deprecated, raw_metadata, last_serial)
VALUES (md5('pkg:torch')::uuid, 'pypi', NULL, 'torch', 'torch', md5('reg:pypi')::uuid, '2.5.1', now(), now(), false, '{}', 7);

INSERT INTO package_registries (package_id, registry_id, first_seen_at, source_url, is_primary)
SELECT id, md5('reg:pypi')::uuid, now(), 'https://pypi.org/project/' || name || '/', true FROM packages;

INSERT INTO package_versions (id, package_id, version, release_date, status, requires_python, features, needed_in_index, raw_metadata)
VALUES
  (md5('ver:flask@3.1.0')::uuid, md5('pkg:flask')::uuid, '3.1.0', now(), 'active', '>=3.9', '{"dependencies_hydrated": true}', false, '{}'),
  (md5('ver:flask@3.0.0')::uuid, md5('pkg:flask')::uuid, '3.0.0', now(), 'active', '>=3.8', '{}', false, '{}'),            -- no key, has dependencies -> hydrated
  (md5('ver:flask@2.0.0')::uuid, md5('pkg:flask')::uuid, '2.0.0', now(), 'yanked', NULL, NULL, false, '{}'),              -- no key, no dependencies -> pending
  (md5('ver:flask@1.0.0')::uuid, md5('pkg:flask')::uuid, '1.0.0', now(), 'active', NULL, '{"dependencies_hydrated": false}', false, '{}'),
  (md5('ver:old@not-a-version')::uuid, md5('pkg:old')::uuid, 'not-a-version', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:jinja2@3.1.4')::uuid, md5('pkg:jinja2')::uuid, '3.1.4', now(), 'active', '>=3.7', '{"dependencies_hydrated": true}', false, '{}'),
  -- PEP 440 ordering cases: pre, local, post, dev, epoch.
  (md5('ver:torch@2.5.1rc1')::uuid, md5('pkg:torch')::uuid, '2.5.1rc1', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:torch@2.5.1')::uuid, md5('pkg:torch')::uuid, '2.5.1', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:torch@2.5.1+cpu')::uuid, md5('pkg:torch')::uuid, '2.5.1+cpu', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:torch@2.5.1.post1')::uuid, md5('pkg:torch')::uuid, '2.5.1.post1', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:torch@2.6.0.dev1')::uuid, md5('pkg:torch')::uuid, '2.6.0.dev1', now(), 'active', NULL, NULL, false, '{}'),
  (md5('ver:torch@1!0.1')::uuid, md5('pkg:torch')::uuid, '1!0.1', now(), 'active', NULL, NULL, false, '{}');

INSERT INTO dependencies (id, version_id, dependency_name, dependency_namespace, dependency_ecosystem, version_constraint,
                          dependency_type, extra_name, environment_markers, raw_spec, resolved_package_id, resolved_version_id)
VALUES
  (md5('dep:1')::uuid, md5('ver:flask@3.1.0')::uuid, 'Jinja2', NULL, 'pypi', '>=3.1.2', 'runtime', NULL, NULL, 'Jinja2>=3.1.2',
   md5('pkg:jinja2')::uuid, md5('ver:jinja2@3.1.4')::uuid),
  (md5('dep:2')::uuid, md5('ver:flask@3.1.0')::uuid, 'typing_extensions', NULL, 'pypi', '*', 'runtime', NULL,
   '{"type": "python_environment", "expression": "python_version < \"3.10\"", "conditions": [{"marker": "python_version", "op": "<", "value": "3.10"}]}',
   'typing_extensions; python_version < "3.10"', NULL, NULL),
  (md5('dep:3')::uuid, md5('ver:flask@3.1.0')::uuid, 'asgiref', NULL, 'pypi', '>=3.2', 'optional', 'async', NULL, 'asgiref>=3.2; extra == "async"', NULL, NULL),
  (md5('dep:4')::uuid, md5('ver:flask@3.0.0')::uuid, 'Werkzeug', NULL, 'pypi', '>=3.0.0', 'runtime', NULL, NULL, 'Werkzeug>=3.0.0', NULL, NULL),
  -- flag false but dependencies present: stays pending (T13).
  (md5('dep:5')::uuid, md5('ver:flask@1.0.0')::uuid, 'Click', NULL, 'pypi', '*', 'runtime', NULL, NULL, 'Click', NULL, NULL),
  -- names of other ecosystems are not normalized.
  (md5('dep:6')::uuid, md5('ver:jinja2@3.1.4')::uuid, 'Left_Pad', NULL, 'npm', '^1.0', 'runtime', NULL, NULL, 'Left_Pad@^1.0', NULL, NULL);

INSERT INTO artifacts (id, version_id, registry_id, filename, download_url, size_bytes, checksum_sha256, artifact_type, requires_dist_metadata)
VALUES
  (md5('art:1')::uuid, md5('ver:flask@3.1.0')::uuid, md5('reg:pypi')::uuid, 'flask-3.1.0-py3-none-any.whl',
   'https://files.pythonhosted.org/flask-3.1.0-py3-none-any.whl', 1000, repeat('a', 64), 'wheel', true),
  (md5('art:2')::uuid, md5('ver:flask@3.1.0')::uuid, md5('reg:pypi')::uuid, 'flask-3.1.0.tar.gz',
   'https://files.pythonhosted.org/flask-3.1.0.tar.gz', 2000, repeat('b', 64), 'sdist', false);

INSERT INTO dist_tags (id, package_id, tag, version, updated_at) VALUES (md5('tag:flask')::uuid, md5('pkg:flask')::uuid, 'latest', '3.1.0', now());

INSERT INTO authors (id, name, email, url) VALUES (md5('author:pallets')::uuid, 'Pallets', 'contact@palletsprojects.com', NULL);
INSERT INTO package_authors (package_id, author_id, role) VALUES (md5('pkg:flask')::uuid, md5('author:pallets')::uuid, 'maintainer');

INSERT INTO vulnerabilities (id, source, source_id, aliases, summary, severity, fixed_in, link)
VALUES (md5('vuln:1')::uuid, 'osv', 'PYSEC-2023-62', ARRAY['CVE-2023-30861'], 'Cookie disclosure', 'high', ARRAY['2.3.2'], 'https://osv.dev/vulnerability/PYSEC-2023-62');
INSERT INTO package_vulnerabilities (package_id, vulnerability_id, affected_versions) VALUES (md5('pkg:flask')::uuid, md5('vuln:1')::uuid, '<2.3.2');

INSERT INTO update_runs (id, started_at, finished_at, status, index_serial, packages_checked, packages_changed, packages_updated, packages_failed, packages_unavailable, events_created)
VALUES (md5('run:1')::uuid, now() - interval '1 day', now() - interval '1 day' + interval '5 minutes', 'success', 42000000, 3, 1, 1, 0, 0, 2);

INSERT INTO sync_failures (id, operation, package_name, error_class, error_message, retryable, attempts)
VALUES (md5('fail:1')::uuid, 'daily_update', 'old-pkg', 'HTTPStatusError', '503 from upstream', true, 2);

INSERT INTO package_events (package_id, version, kind, details, run_id)
VALUES (md5('pkg:flask')::uuid, NULL, 'tracking_started', '{"latest_version": "3.1.0"}', NULL),
       (md5('pkg:flask')::uuid, '3.1.0', 'release_added', '{"bump": "minor", "previous_version": "3.0.0"}', md5('run:1')::uuid);

COMMIT;
