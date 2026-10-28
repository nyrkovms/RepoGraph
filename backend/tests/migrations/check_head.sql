-- Invariants that must hold after `alembic upgrade head` on seed_0005.sql (IMPL §2 "Проверка").
-- Migration WPs extend this file with checks for their own transformations (0006: namespace, sort_key,
-- version_sources, dependencies_state...).
DO $$
BEGIN
  IF (SELECT count(*) FROM packages) <> 3 THEN RAISE EXCEPTION 'packages count changed'; END IF;
  IF (SELECT count(*) FROM package_versions) <> 6 THEN RAISE EXCEPTION 'package_versions count changed'; END IF;
  IF (SELECT count(*) FROM dependencies) <> 4 THEN RAISE EXCEPTION 'dependencies count changed'; END IF;
  IF (SELECT count(*) FROM artifacts) <> 2 THEN RAISE EXCEPTION 'artifacts count changed'; END IF;
  IF (SELECT count(*) FROM package_vulnerabilities) <> 1 THEN RAISE EXCEPTION 'vulnerability links lost'; END IF;
  IF (SELECT count(*) FROM package_authors) <> 1 THEN RAISE EXCEPTION 'author links lost'; END IF;
  IF (SELECT count(*) FROM package_events) <> 2 THEN RAISE EXCEPTION 'events lost'; END IF;
  RAISE NOTICE 'ok: migration invariants hold';
END $$;

-- 0006 (expand): IMPL §2 and §1.2 T12–T16.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM packages WHERE namespace IS NULL) THEN RAISE EXCEPTION '0006: NULL namespace left'; END IF;
  IF (SELECT origin::text FROM packages WHERE normalized_name = 'flask') <> 'seed' THEN RAISE EXCEPTION '0006: flask origin'; END IF;
  IF (SELECT origin::text FROM packages WHERE normalized_name = 'jinja2') <> 'dependency' THEN RAISE EXCEPTION '0006: jinja2 origin'; END IF;
  IF (SELECT deprecation->>'message' FROM packages WHERE normalized_name = 'old-pkg') IS DISTINCT FROM 'use new-pkg' THEN
    RAISE EXCEPTION '0006: deprecation not copied'; END IF;
  IF (SELECT protocol::text || '/' || priority || '/' || enabled FROM registries WHERE slug = 'pypi') <> 'pypi-warehouse/0/true' THEN
    RAISE EXCEPTION '0006: registry not filled'; END IF;
  IF EXISTS (SELECT 1 FROM package_registries WHERE ecosystem IS NULL) THEN RAISE EXCEPTION '0006: package_registries.ecosystem'; END IF;
  IF (SELECT upstream_revision FROM package_registries pr JOIN packages p ON p.id = pr.package_id WHERE p.normalized_name = 'flask') <> '100' THEN
    RAISE EXCEPTION '0006: last_serial not copied'; END IF;
  IF EXISTS (SELECT 1 FROM package_versions WHERE sort_key IS NULL OR is_prerelease IS NULL) THEN RAISE EXCEPTION '0006: sort_key missing'; END IF;
  IF (SELECT array_agg(version ORDER BY sort_key) FROM package_versions v JOIN packages p ON p.id = v.package_id WHERE p.normalized_name = 'flask')
     <> ARRAY['1.0.0', '2.0.0', '3.0.0', '3.1.0'] THEN RAISE EXCEPTION '0006: sort_key order'; END IF;
  IF (SELECT get_byte(sort_key, 0) || '/' || (raw_metadata->>'invalid_version') FROM package_versions WHERE version = 'not-a-version') <> '0/true' THEN
    RAISE EXCEPTION '0006: invalid version fallback'; END IF;
  IF (SELECT string_agg(version || ':' || dependencies_state, ',' ORDER BY sort_key) FROM package_versions v JOIN packages p ON p.id = v.package_id WHERE p.normalized_name = 'flask')
     <> '1.0.0:pending,2.0.0:pending,3.0.0:hydrated,3.1.0:hydrated' THEN RAISE EXCEPTION '0006: dependencies_state mapping'; END IF;
  IF (SELECT engines->>'python' FROM package_versions WHERE version = '3.1.0') <> '>=3.9' THEN RAISE EXCEPTION '0006: engines'; END IF;
  IF (SELECT count(*) FROM version_sources) <> 6 THEN RAISE EXCEPTION '0006: version_sources'; END IF;
  IF EXISTS (SELECT 1 FROM package_versions WHERE metadata_registry_id IS NULL) THEN RAISE EXCEPTION '0006: metadata_registry_id'; END IF;
  IF (SELECT array_agg(dependency_name ORDER BY dependency_name) FROM dependencies) <> ARRAY['asgiref', 'jinja2', 'typing-extensions', 'werkzeug'] THEN
    RAISE EXCEPTION '0006: dependency names not normalized'; END IF;
  IF EXISTS (SELECT 1 FROM dependencies WHERE dependency_namespace IS NULL) THEN RAISE EXCEPTION '0006: dependency namespace'; END IF;
  IF (SELECT count(*) FROM artifacts WHERE has_metadata_file) <> 1 THEN RAISE EXCEPTION '0006: has_metadata_file'; END IF;
  IF (SELECT per_registry->'pypi'->>'index_serial' FROM runs WHERE operation = 'daily_update') <> '42000000' THEN RAISE EXCEPTION '0006: runs'; END IF;
  IF (SELECT package_ref || '/' || ecosystem FROM sync_failures) <> 'pkg:pypi/old-pkg/pypi' THEN RAISE EXCEPTION '0006: sync_failures'; END IF;
  IF (SELECT count(*) FROM source_profile_registries spr JOIN source_profiles sp ON sp.id = spr.profile_id WHERE sp.is_default AND sp.ecosystem = 'pypi') <> 1 THEN
    RAISE EXCEPTION '0006: default profile'; END IF;
  RAISE NOTICE 'ok: 0006 transformations';
END $$;
