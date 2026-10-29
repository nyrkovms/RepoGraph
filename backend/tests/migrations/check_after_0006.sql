-- After seed_0005.sql -> 0006 -> seed_after_0006.sql -> head: 0007 caught up with what was written in between
-- (IMPL §2) and kept what the current code wrote.
DO $$
BEGIN
  IF (SELECT encode(sort_key, 'hex') IS NULL OR NOT EXISTS (SELECT 1 FROM version_sources WHERE version_id = v.id)
      FROM package_versions v WHERE id = md5('ver:flask@3.2.0')::uuid) THEN RAISE EXCEPTION '0007: new version not backfilled'; END IF;
  IF (SELECT engines->>'python' || '/' || dependencies_state FROM package_versions WHERE id = md5('ver:flask@3.2.0')::uuid) <> '>=3.10/hydrated' THEN
    RAISE EXCEPTION '0007: new version engines/state'; END IF;
  IF (SELECT namespace FROM packages WHERE id = md5('pkg:click')::uuid) <> '' THEN RAISE EXCEPTION '0007: namespace'; END IF;
  IF (SELECT ecosystem::text || '/' || upstream_revision FROM package_registries WHERE package_id = md5('pkg:click')::uuid) <> 'pypi/5' THEN
    RAISE EXCEPTION '0007: package_registries of a new package'; END IF;
  IF (SELECT engines->>'python' FROM package_versions WHERE id = md5('ver:torch@2.5.1')::uuid) IS DISTINCT FROM '>=3.9' THEN
    RAISE EXCEPTION '0007: engines from requires_python'; END IF;
  IF (SELECT engines->>'python' FROM package_versions WHERE id = md5('ver:jinja2@3.1.4')::uuid) <> '>=3.11' THEN
    RAISE EXCEPTION '0007: engines overwritten by a stale requires_python'; END IF;
  IF (SELECT dependencies_state::text FROM package_versions WHERE id = md5('ver:flask@1.0.0')::uuid) <> 'hydrated' THEN
    RAISE EXCEPTION '0007: dependencies_state from the flag'; END IF;
  IF (SELECT upstream_revision FROM package_registries WHERE package_id = md5('pkg:flask')::uuid) <> '150' THEN
    RAISE EXCEPTION '0007: upstream_revision from a newer last_serial'; END IF;
  IF (SELECT upstream_revision FROM package_registries WHERE package_id = md5('pkg:torch')::uuid) <> '9' THEN
    RAISE EXCEPTION '0007: upstream_revision overwritten by an older last_serial'; END IF;
  IF (SELECT status || '/' || (finished_at IS NOT NULL) FROM runs WHERE id = md5('run:1')::uuid) <> 'success/true' THEN
    RAISE EXCEPTION '0007: unfinished run not re-synced'; END IF;
  IF (SELECT status || '/' || (per_registry->'pypi'->>'index_serial') FROM runs WHERE id = md5('run:2')::uuid) <> 'partial/42000100' THEN
    RAISE EXCEPTION '0007: update_runs row written after 0006 not copied'; END IF;
  IF (SELECT run_id FROM package_events WHERE version = '3.2.0') <> md5('run:2')::uuid THEN RAISE EXCEPTION '0007: event run_id'; END IF;
  IF EXISTS (SELECT 1 FROM package_events WHERE kind = 'project_restored' AND run_id IS NOT NULL) THEN
    RAISE EXCEPTION '0007: dangling run_id kept'; END IF;
  IF (SELECT string_agg(package_ref || '/' || attempts, ',') FROM sync_failures WHERE operation = 'seed') <> 'pkg:pypi/some-pkg/3' THEN
    RAISE EXCEPTION '0007: sync_failures duplicates under the new key'; END IF;
  RAISE NOTICE 'ok: 0007 caught up with writes after 0006';
END $$;
