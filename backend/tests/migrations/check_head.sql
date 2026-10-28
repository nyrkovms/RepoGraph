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
