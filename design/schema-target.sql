-- PackageHub: target schema for multi-ecosystem catalog (SPEC-MULTI-ECOSYSTEM.md §5).
-- Design artifact: the real change ships as Alembic migrations 0006/0007.
-- Only tables touched by the multi-ecosystem work are listed; authors, vulnerabilities,
-- resolve cache and the rest stay as they are.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TYPE ecosystem_enum AS ENUM ('pypi', 'npm', 'maven', 'go', 'crates', 'nuget', 'rpm');

-- How we talk to a source. One ecosystem can have several protocols (pypi.org vs a PEP 503 index).
CREATE TYPE registry_protocol_enum AS ENUM (
    'pypi-warehouse',   -- pypi.org: JSON API + Simple API with serials
    'pypi-simple',      -- PEP 503/691 only: download.pytorch.org, pypi.nvidia.com, devpi
    'npm-registry',     -- registry.npmjs.org and compatible registries
    'maven2',           -- Maven repository layout: Central, Google, Gradle Plugin Portal, JBoss, ...
    'nuget-v3',
    'goproxy',
    'cargo-sparse',
    'rpm-repodata'
);

CREATE TYPE package_origin_enum AS ENUM ('seed', 'manual', 'dependency', 'discovered');

-- 'deprecated' is gone from the lifecycle: deprecation is orthogonal (a version can be yanked and deprecated).
CREATE TYPE version_status_enum AS ENUM ('active', 'yanked', 'unlisted', 'removed', 'superseded');

CREATE TYPE dependency_type_enum AS ENUM ('runtime', 'dev', 'optional', 'build', 'peer', 'provided');

CREATE TYPE dependencies_state_enum AS ENUM ('pending', 'hydrated', 'unavailable');


-- Sources -----------------------------------------------------------------------------------------

CREATE TABLE registries (
    id              uuid PRIMARY KEY,
    ecosystem       ecosystem_enum NOT NULL,
    slug            varchar(64) NOT NULL UNIQUE,          -- pypi, pytorch-cu121, maven-central, gradle-plugins, rhel-9-appstream
    name            text NOT NULL,
    protocol        registry_protocol_enum NOT NULL,
    base_url        text NOT NULL,
    config          jsonb NOT NULL DEFAULT '{}'::jsonb,   -- protocol options: changes feed URL, RPM distro/release/arch, namespace, eol
    routing         jsonb,                                -- which packages this source serves (§5.4); NULL = any
    priority        integer NOT NULL DEFAULT 100,         -- lower = consulted first
    enabled         boolean NOT NULL DEFAULT true,
    auth_type       varchar(32) NOT NULL DEFAULT 'none' CHECK (auth_type IN ('none', 'basic', 'bearer', 'tls_client_cert')),
    credentials_ref text,                                 -- name of a secret, never the secret itself
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, ecosystem),                               -- target for composite FKs: a source of the same ecosystem
    CHECK (auth_type = 'none' OR credentials_ref IS NOT NULL)
);

-- Ordered set of sources used for discovery, the dependency tree and resolve (pip --index-url/--extra-index-url,
-- Maven <repositories>, npm scope registries).
CREATE TABLE source_profiles (
    id          uuid PRIMARY KEY,
    ecosystem   ecosystem_enum NOT NULL,
    slug        varchar(64) NOT NULL,
    name        text NOT NULL,
    is_default  boolean NOT NULL DEFAULT false,
    UNIQUE (ecosystem, slug),
    UNIQUE (id, ecosystem)
);
CREATE UNIQUE INDEX uq_source_profiles_default ON source_profiles (ecosystem) WHERE is_default;

CREATE TABLE source_profile_registries (
    profile_id       uuid NOT NULL,
    registry_id      uuid NOT NULL,
    ecosystem        ecosystem_enum NOT NULL,
    position         integer NOT NULL,
    routing_override jsonb,
    PRIMARY KEY (profile_id, registry_id),
    UNIQUE (profile_id, position),
    FOREIGN KEY (profile_id, ecosystem) REFERENCES source_profiles (id, ecosystem) ON DELETE CASCADE,
    FOREIGN KEY (registry_id, ecosystem) REFERENCES registries (id, ecosystem)
);


-- Catalog -----------------------------------------------------------------------------------------

CREATE TABLE packages (
    id                      uuid PRIMARY KEY,
    ecosystem               ecosystem_enum NOT NULL,
    namespace               varchar(255) NOT NULL DEFAULT '',   -- '' instead of NULL: NULLs never collide in a UNIQUE index
    name                    text NOT NULL,
    normalized_name         text NOT NULL,
    origin                  package_origin_enum NOT NULL,
    primary_registry_id     uuid NOT NULL,                      -- highest-priority source that has the package; metadata comes from it
    description             text,
    homepage_url            text,
    repository_url          text,
    documentation_url       text,
    license_raw             text,
    license_spdx            varchar(255),
    keywords                text[],
    latest_version          text,                               -- NULL: a project without releases
    deprecation             jsonb,                              -- {"message", "alternative"}: npm, NuGet, Maven relocation
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL,
    synced_at               timestamptz NOT NULL,
    unavailable_at          timestamptz,
    raw_metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ecosystem, namespace, normalized_name),
    UNIQUE (id, ecosystem),
    FOREIGN KEY (primary_registry_id, ecosystem) REFERENCES registries (id, ecosystem)
);
CREATE INDEX idx_pkg_name_trgm ON packages USING gin (normalized_name gin_trgm_ops);
CREATE INDEX idx_pkg_license ON packages (license_spdx) WHERE license_spdx IS NOT NULL;

-- Where a package lives and the per-source change-detection state.
CREATE TABLE package_registries (
    package_id         uuid NOT NULL,
    registry_id        uuid NOT NULL,
    ecosystem          ecosystem_enum NOT NULL,
    first_seen_at      timestamptz NOT NULL,
    missing_since      timestamptz,                    -- 404 / gone from repodata in this source
    source_url         text,
    upstream_revision  text,                           -- serial, ETag, maven lastUpdated, EVR, catalog commit
    etag               text,
    last_modified      text,
    checked_at         timestamptz,
    PRIMARY KEY (package_id, registry_id),
    FOREIGN KEY (package_id, ecosystem) REFERENCES packages (id, ecosystem) ON DELETE CASCADE,
    FOREIGN KEY (registry_id, ecosystem) REFERENCES registries (id, ecosystem)
);
CREATE INDEX idx_pkgreg_registry ON package_registries (registry_id);

CREATE TABLE package_versions (
    id                   uuid PRIMARY KEY,
    package_id           uuid NOT NULL REFERENCES packages (id) ON DELETE CASCADE,
    version              text NOT NULL,
    sort_key             bytea NOT NULL,                 -- VersionScheme.sort_key: byte order == ecosystem order (§5.3)
    is_prerelease        boolean NOT NULL,
    release_date         timestamptz,
    status               version_status_enum NOT NULL DEFAULT 'active',  -- aggregate over sources, primary wins
    status_reason        text,                           -- yank reason, Go retract rationale
    deprecation          jsonb,
    engines              jsonb,                          -- {"python": ">=3.9"}, {"node": ">=18"}, {"rust": "1.70"}, {"go": "1.22"}
    features             jsonb,                          -- crates features map, PyPI extras
    metadata_registry_id uuid REFERENCES registries (id),-- source the dependencies were read from
    dependencies_state   dependencies_state_enum NOT NULL DEFAULT 'pending',
    raw_metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- RPM: module, sourcerpm; Maven: managed deps
    UNIQUE (package_id, version)
);
CREATE INDEX idx_ver_pkg_sort ON package_versions (package_id, sort_key DESC);
CREATE INDEX idx_ver_release ON package_versions (release_date);

-- Which sources publish a version, with the per-source status. Drives profile-aware resolve.
CREATE TABLE version_sources (
    version_id       uuid NOT NULL REFERENCES package_versions (id) ON DELETE CASCADE,
    registry_id      uuid NOT NULL REFERENCES registries (id),
    first_seen_at    timestamptz NOT NULL,
    status           version_status_enum NOT NULL DEFAULT 'active',
    metadata_digest  bytea,                              -- hash of normalized dependencies; differs from canonical => variant rows in dependencies
    PRIMARY KEY (version_id, registry_id)
);
CREATE INDEX idx_versrc_registry ON version_sources (registry_id);

CREATE TABLE artifacts (
    id                  uuid PRIMARY KEY,
    version_id          uuid NOT NULL REFERENCES package_versions (id) ON DELETE CASCADE,
    registry_id         uuid NOT NULL REFERENCES registries (id),
    filename            text NOT NULL,
    download_url        text,                            -- NULL when derived from registries.config url pattern
    size_bytes          bigint,
    checksum_sha256     varchar(64),
    checksum_sha512     varchar(128),
    checksum_sha1       varchar(40),
    checksum_md5        varchar(32),
    checksum_integrity  text,                            -- npm SRI
    artifact_type       varchar(32) NOT NULL,            -- wheel, sdist, tgz, jar, pom, module, nupkg, crate, go_mod, go_zip, rpm
    arch                varchar(32),                     -- RPM
    platform_tags       jsonb,
    has_metadata_file   boolean NOT NULL DEFAULT false,  -- PEP 658 .metadata next to a wheel, Maven .module next to POM
    metadata_blob       text,                            -- POM, go.mod: needed again by the resolver
    UNIQUE (version_id, registry_id, filename)
);
CREATE INDEX idx_art_version ON artifacts (version_id);

CREATE TABLE dependencies (
    id                    uuid PRIMARY KEY,
    version_id            uuid NOT NULL REFERENCES package_versions (id) ON DELETE CASCADE,
    dependency_ecosystem  ecosystem_enum NOT NULL,
    dependency_namespace  varchar(255) NOT NULL DEFAULT '',
    dependency_name       text NOT NULL,                 -- real package name; RPM: capability
    alias                 text,                          -- npm "npm:" alias key, Cargo `package =` rename
    version_constraint    text NOT NULL,
    dependency_type       dependency_type_enum NOT NULL,
    scope                 varchar(32),                   -- native kind: compile/test, devDependencies, requires/recommends
    extra_name            varchar(128),                  -- PyPI extra, Cargo feature enabling an optional dep
    environment_markers   jsonb,                         -- typed: python_environment, dotnet_target_framework, rust_cfg, npm_platform, rpm_arch
    details               jsonb,                         -- exclusions, classifier, features, default-features, peer optional, RPM flags/evr
    raw_spec              text NOT NULL,
    -- NULL: the canonical set (package_versions.metadata_registry_id and every source with the same metadata_digest).
    -- Set only for a source whose metadata for this version differs (torch 2.9.1 on PyPI vs the PyTorch index).
    variant_registry_id   uuid REFERENCES registries (id),
    resolved_package_id   uuid REFERENCES packages (id),
    resolved_version_id   uuid REFERENCES package_versions (id)
);
CREATE INDEX idx_dep_version ON dependencies (version_id, variant_registry_id);
CREATE INDEX idx_dep_target ON dependencies (dependency_ecosystem, dependency_namespace, dependency_name);
CREATE INDEX idx_dep_resolved ON dependencies (resolved_version_id) WHERE resolved_version_id IS NOT NULL;

-- Auxiliary upstream documents that belong to no catalog version: Maven parent POMs and BOMs, go.mod files
-- read during MVS, PEP 658 metadata files. artifacts.version_id cannot hold them.
CREATE TABLE source_documents (
    registry_id    uuid NOT NULL REFERENCES registries (id),
    path           text NOT NULL,                        -- relative to base_url: org/x/y/1.0/y-1.0.pom, github.com/a/b/@v/v1.2.3.mod
    content        bytea NOT NULL,
    content_type   varchar(64) NOT NULL,
    sha256         varchar(64) NOT NULL,
    etag           text,
    fetched_at     timestamptz NOT NULL,
    PRIMARY KEY (registry_id, path)
);

CREATE TABLE dist_tags (
    package_id  uuid NOT NULL REFERENCES packages (id) ON DELETE CASCADE,
    tag         varchar(64) NOT NULL,
    version     text NOT NULL,
    updated_at  timestamptz NOT NULL,
    PRIMARY KEY (package_id, tag)
);


-- Operations --------------------------------------------------------------------------------------

CREATE TABLE sync_states (                                -- one row per source: feed cursors
    registry_id     uuid PRIMARY KEY REFERENCES registries (id),
    cursor_data     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {"last_serial"}, {"last_seq"}, {"commit_ts"}, {"since"}, {"repomd_revision"}
    status          varchar(16) NOT NULL DEFAULT 'idle',
    last_success_at timestamptz,
    last_error      text
);

CREATE TABLE update_runs (
    id                uuid PRIMARY KEY,
    ecosystem         ecosystem_enum NOT NULL,
    status            varchar(16) NOT NULL,
    started_at        timestamptz NOT NULL,
    finished_at       timestamptz,
    packages_checked  integer NOT NULL DEFAULT 0,
    packages_changed  integer NOT NULL DEFAULT 0,
    packages_failed   integer NOT NULL DEFAULT 0,
    per_registry      jsonb NOT NULL DEFAULT '{}'::jsonb, -- {"pypi": {...}, "pytorch-cu121": {...}}
    events_by_kind    jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_update_runs_eco_started ON update_runs (ecosystem, started_at DESC);

CREATE TABLE sync_failures (
    id             uuid PRIMARY KEY,
    operation      varchar(32) NOT NULL,
    ecosystem      ecosystem_enum NOT NULL,
    registry_id    uuid REFERENCES registries (id),       -- NULL when no single source is to blame
    package_ref    text NOT NULL,                         -- purl without version
    error_class    text NOT NULL,
    error_message  text NOT NULL,
    retryable      boolean NOT NULL DEFAULT true,
    attempts       integer NOT NULL DEFAULT 1,
    next_retry_at  timestamptz,
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (operation, ecosystem, registry_id, package_ref)
);

-- package_events keeps its shape (SPEC-DAILY-UPDATE §3.3); new kinds: release_deprecated.
-- The ecosystem filter of the digest joins packages.
