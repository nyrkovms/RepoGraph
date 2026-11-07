import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { fetchCatalogSummary, fetchPackages, searchPackages } from "../lib/api";
import { isPurl, purlDestination } from "../lib/purl";
import { getPackageDescriptionPreview } from "../lib/packageDescription";
import { ECOSYSTEM_OPTIONS, displayName, ecosystemLabel, itemKey, packageHref } from "../lib/packageNames";
import SearchBox from "../lib/SearchBox";
import { formatDate } from "../lib/updates";

const DEFAULT_PLACEHOLDER = "flask, requests, numpy...";
// A Maven package is found and opened by `groupId:artifactId`.
// An RPM package is found and opened by `distro/name`: the distro and release are its namespace (`openssl` in
// `rocky-9` and in `rhel-9` are two packages).
const PLACEHOLDERS = {
  maven: "group:artifact, e.g. org.springframework:spring-core",
  rpm: "distro/name, e.g. rocky-9/openssl",
};

const PAGE_SIZE = 20;
const SORT_OPTIONS = [
  { value: "relevance", label: "Relevance" },
  { value: "used_by", label: "Most used" },
  { value: "name", label: "Name" },
];

// One list request from the search spec: the query decides between /search and /packages.
function requestPage(spec, cursor) {
  const { q, ...filters } = spec;
  const set = Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== ""));
  const params = { limit: PAGE_SIZE, ...set, ...(cursor ? { cursor } : {}) };
  return q ? searchPackages({ q, ...params }) : fetchPackages(params);
}

export default function SearchPage() {
  // The view lives in the URL so a link can be shared; a default adds nothing to it.
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const q = (params.get("q") || "").trim();
  const ecosystemParam = params.get("ecosystem") || "";
  const ecosystem = ECOSYSTEM_OPTIONS.some((option) => option.value && option.value === ecosystemParam) ? ecosystemParam : "";
  const license = params.get("license") || "";
  const minUsedBy = /^[1-9]\d*$/.test(params.get("min_used_by") || "") ? params.get("min_used_by") : "";
  const releasedBeforeParam = params.get("released_before") || "";
  const releasedBefore = Number.isNaN(Date.parse(releasedBeforeParam)) ? "" : releasedBeforeParam;
  const deprecated = ["true", "1"].includes(params.get("deprecated") || "");
  // Relevance ranks the matches of a query: without one the list is by name.
  const sortParam = params.get("sort");
  const defaultSort = q ? "relevance" : "name";
  const sort = sortParam === "used_by" || sortParam === "name" || (sortParam === "relevance" && q) ? sortParam : defaultSort;

  const spec = {
    q,
    sort,
    ecosystem,
    license,
    min_used_by: minUsedBy,
    released_before: releasedBefore,
    deprecated: deprecated ? true : "",
  };
  const key = JSON.stringify(spec);

  const [draft, setDraft] = useState(q);
  const [seenQuery, setSeenQuery] = useState(q);
  if (seenQuery !== q) {
    // The address changed (back, a link): the field follows it.
    setSeenQuery(q);
    setDraft(q);
  }

  // `key` says which request the stored list answers: a different key means the list is still loading.
  const [result, setResult] = useState({ key: null, rows: [], cursor: null, error: "" });
  const [loadingMore, setLoadingMore] = useState(false);
  const [licenses, setLicenses] = useState([]);
  const loading = result.key !== key;

  useEffect(() => {
    let cancelled = false;
    requestPage(JSON.parse(key))
      .then((payload) => {
        if (!cancelled) setResult({ key, rows: payload.data || [], cursor: payload.cursor || null, error: "" });
      })
      .catch((err) => {
        if (!cancelled) setResult({ key, rows: [], cursor: null, error: err?.message || "Search request failed" });
      });
    return () => {
      cancelled = true;
    };
  }, [key]);

  useEffect(() => {
    let cancelled = false;
    fetchCatalogSummary()
      .then((payload) => {
        if (!cancelled) setLicenses((payload.licenses || []).map((item) => item.license));
      })
      .catch(() => {
        // The select then offers only what the address names: the list itself still works.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function setParam(name, value, { replace = true } = {}) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace }
    );
  }

  async function loadMore() {
    if (!result.cursor || loadingMore) {
      return;
    }
    setLoadingMore(true);
    try {
      const payload = await requestPage(spec, result.cursor);
      setResult((prev) =>
        prev.key === key ? { ...prev, rows: [...prev.rows, ...(payload.data || [])], cursor: payload.cursor || null } : prev
      );
    } catch (err) {
      setResult((prev) => (prev.key === key ? { ...prev, error: err?.message || "Pagination request failed" } : prev));
    } finally {
      setLoadingMore(false);
    }
  }

  async function handleSubmit() {
    const typed = draft.trim();
    if (isPurl(typed)) {
      // A purl names one package: open its card (at the purl's version) as the home page does; with none or several the
      // results page says so.
      try {
        const href = await purlDestination(typed);
        if (href) {
          navigate(href);
          return;
        }
      } catch {
        // The list below reports a failing search itself.
      }
    }
    setParam("q", typed, { replace: false });
  }

  const licenseOptions = license && !licenses.includes(license) ? [license, ...licenses] : licenses;
  // Filters the page has no control for are shown as chips, so a list that arrives short says why.
  const chips = [
    minUsedBy ? { name: "min_used_by", label: `used by ${minUsedBy}+` } : null,
    releasedBefore ? { name: "released_before", label: `latest release before ${formatDate(releasedBefore)}` } : null,
    deprecated ? { name: "deprecated", label: "deprecated" } : null,
  ].filter(Boolean);

  return (
    <section>
      <h2>Search packages</h2>
      <div className="toolbar">
        <label>
          Ecosystem
          <select aria-label="Ecosystem" value={ecosystem} onChange={(e) => setParam("ecosystem", e.target.value)}>
            {ECOSYSTEM_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <SearchBox
          value={draft}
          onChange={setDraft}
          onSubmit={handleSubmit}
          placeholder={PLACEHOLDERS[ecosystem] || DEFAULT_PLACEHOLDER}
          ecosystem={ecosystem}
          applied={q}
        />
        <button type="button" onClick={handleSubmit} disabled={loading}>
          {loading ? "Loading..." : "Search"}
        </button>
      </div>
      <div className="toolbar">
        <label>
          Sort
          <select
            aria-label="Sort"
            value={sort}
            onChange={(e) => setParam("sort", e.target.value === defaultSort ? "" : e.target.value)}
          >
            {SORT_OPTIONS.filter((option) => option.value !== "relevance" || q).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          License
          <select aria-label="License" value={license} onChange={(e) => setParam("license", e.target.value)}>
            <option value="">All licenses</option>
            {licenseOptions.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
      </div>

      {chips.length ? (
        <ul className="filter-chips" aria-label="Active filters">
          {chips.map((chip) => (
            <li key={chip.name} className="filter-chip">
              <span>{chip.label}</span>
              <button
                type="button"
                className="chip-clear"
                aria-label={`Clear filter: ${chip.label}`}
                onClick={() => setParam(chip.name, "")}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {result.error ? <p className="error">{result.error}</p> : null}

      <ul className="result-list">
        {(loading ? [] : result.rows).map((item) => {
          const description = getPackageDescriptionPreview(item.description, 220);
          return (
            <li key={itemKey(item)}>
              <Link to={packageHref(item)}>{displayName(item)}</Link>
              <span className="tag eco-badge">{ecosystemLabel(item.ecosystem)}</span>
              <span className="tag">{item.latest_version}</span>
              {item.license_spdx ? <span className="tag">{item.license_spdx}</span> : null}
              {item.has_vulnerabilities ? <span className="tag warn">vuln</span> : null}
              {item.used_by > 0 ? (
                <span className="muted used-by" title="Packages in this catalog that depend on it">
                  used by {item.used_by}
                </span>
              ) : null}
              {description ? <p>{description}</p> : null}
            </li>
          );
        })}
      </ul>
      {!loading && result.rows.some((item) => item.used_by > 0) ? (
        <p className="muted home-note">used by counts packages in this catalog</p>
      ) : null}

      {!loading && !result.error && !result.rows.length ? (
        <div className="empty-state">
          <p>
            No packages match{q ? ` "${q}"` : ""}
          </p>
          {isPurl(q) ? <p className="muted">No package matches this purl</p> : null}
        </div>
      ) : null}

      {result.cursor && !loading ? (
        <button type="button" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading..." : "Load more"}
        </button>
      ) : null}
    </section>
  );
}
