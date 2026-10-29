-- Writes between 0006 and 0007 (IMPL §2 "Повторное заполнение в 0007"), on top of seed_0005.sql at 0006:
-- what the 0005-era code leaves only in the old columns, and what the current code changes only in the new ones.
BEGIN;

-- 0005-era code: a new release without sort_key, engines or version_sources, and a package without namespace.
INSERT INTO package_versions (id, package_id, version, release_date, status, requires_python, features, needed_in_index, raw_metadata)
VALUES (md5('ver:flask@3.2.0')::uuid, md5('pkg:flask')::uuid, '3.2.0', now(), 'active', '>=3.10', '{"dependencies_hydrated": true}', false, '{}');
INSERT INTO packages (id, ecosystem, namespace, name, normalized_name, primary_registry_id, latest_version,
                      updated_at, synced_at, deprecated, raw_metadata, last_serial)
VALUES (md5('pkg:click')::uuid, 'pypi', NULL, 'click', 'click', md5('reg:pypi')::uuid, '8.1.7', now(), now(), false, '{}', 5);
INSERT INTO package_registries (package_id, registry_id, first_seen_at, is_primary)
VALUES (md5('pkg:click')::uuid, md5('reg:pypi')::uuid, now(), true);

-- 0005-era code: requires_python of a version without engines, the hydration flag, a newer serial.
UPDATE package_versions SET requires_python = '>=3.9' WHERE id = md5('ver:torch@2.5.1')::uuid;
UPDATE package_versions SET features = '{"dependencies_hydrated": true}' WHERE id = md5('ver:flask@1.0.0')::uuid;
UPDATE packages SET last_serial = 150 WHERE id = md5('pkg:flask')::uuid;

-- Current code: engines and the revision moved on, the old columns are stale and must not be copied back.
UPDATE package_versions SET engines = '{"python": ">=3.11"}' WHERE id = md5('ver:jinja2@3.1.4')::uuid;
UPDATE package_registries SET upstream_revision = '9' WHERE package_id = md5('pkg:torch')::uuid;

-- A run copied while still running, finished afterwards in update_runs; a new run of the 0005-era code with an
-- event; an event pointing at no run.
UPDATE runs SET status = 'running', finished_at = NULL WHERE id = md5('run:1')::uuid;
INSERT INTO update_runs (id, started_at, finished_at, status, index_serial, packages_checked, packages_changed, packages_updated, packages_failed, packages_unavailable, events_created)
VALUES (md5('run:2')::uuid, now() - interval '1 hour', now(), 'partial', 42000100, 4, 1, 1, 1, 0, 1);
INSERT INTO package_events (package_id, version, kind, details, run_id)
VALUES (md5('pkg:flask')::uuid, '3.2.0', 'release_added', '{}', md5('run:2')::uuid),
       (md5('pkg:flask')::uuid, NULL, 'project_restored', '{}', md5('run:none')::uuid);

-- 0005-era code: failures by name only, two spellings of one package (one row under the new key).
INSERT INTO sync_failures (id, operation, package_name, error_class, error_message, retryable, attempts, last_seen_at)
VALUES (md5('fail:2')::uuid, 'seed', 'Some_Pkg', 'ReadTimeout', 'timeout', true, 1, now() - interval '1 hour'),
       (md5('fail:3')::uuid, 'seed', 'some-pkg', 'ReadTimeout', 'timeout', true, 3, now());

COMMIT;
