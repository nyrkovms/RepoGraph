import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { fetchPackages, searchPackages } from "../lib/api";
import { getPackageDescriptionPreview } from "../lib/packageDescription";
import { ECOSYSTEM_OPTIONS, displayName, ecosystemLabel, itemKey, packageHref } from "../lib/packageNames";
import SearchBox from "../lib/SearchBox";

const DEFAULT_PLACEHOLDER = "flask, requests, numpy...";
// A Maven package is found and opened by `groupId:artifactId`.
// An RPM package is found and opened by `distro/name`: the distro and release are its namespace (`openssl` in
// `rocky-9` and in `rhel-9` are two packages).
const PLACEHOLDERS = {
  maven: "group:artifact, e.g. org.springframework:spring-core",
  rpm: "distro/name, e.g. rocky-9/openssl",
};

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [ecosystem, setEcosystem] = useState("");
  const [rows, setRows] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // The list is reloaded on mount and whenever the ecosystem switcher changes.
  useEffect(() => {
    loadFirstPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ecosystem]);

  function ecosystemFilter() {
    return ecosystem ? { ecosystem } : {};
  }

  async function loadFirstPage() {
    setLoading(true);
    setError("");
    try {
      const payload = query.trim()
        ? await searchPackages({ q: query.trim(), limit: 20, sort: "relevance", ...ecosystemFilter() })
        : await fetchPackages({ limit: 20, sort: "name", ...ecosystemFilter() });
      setRows(payload.data || []);
      setCursor(payload.cursor || null);
    } catch (err) {
      setError(err?.message || "Search request failed");
    } finally {
      setLoading(false);
    }
  }

  async function loadMore() {
    if (!cursor) {
      return;
    }
    setLoading(true);
    setError("");
    try {
      const payload = query.trim()
        ? await searchPackages({ q: query.trim(), limit: 20, sort: "relevance", ...ecosystemFilter(), cursor })
        : await fetchPackages({ limit: 20, sort: "name", ...ecosystemFilter(), cursor });
      setRows((prev) => [...prev, ...(payload.data || [])]);
      setCursor(payload.cursor || null);
    } catch (err) {
      setError(err?.message || "Pagination request failed");
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit() {
    loadFirstPage();
  }

  return (
    <section>
      <h2>Search packages</h2>
      <div className="toolbar">
        <label>
          Ecosystem
          <select aria-label="Ecosystem" value={ecosystem} onChange={(e) => setEcosystem(e.target.value)}>
            {ECOSYSTEM_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <SearchBox
          value={query}
          onChange={setQuery}
          onSubmit={handleSubmit}
          placeholder={PLACEHOLDERS[ecosystem] || DEFAULT_PLACEHOLDER}
          ecosystem={ecosystem}
        />
        <button type="button" onClick={handleSubmit} disabled={loading}>
          {loading ? "Loading..." : "Search"}
        </button>
      </div>

      {error ? <p className="error">{error}</p> : null}

      <ul className="result-list">
        {rows.map((item) => (
          <li key={itemKey(item)}>
            <Link to={packageHref(item)}>{displayName(item)}</Link>
            <span className="tag eco-badge">{ecosystemLabel(item.ecosystem)}</span>
            <span className="tag">{item.latest_version}</span>
            {item.license_spdx ? <span className="tag">{item.license_spdx}</span> : null}
            {item.has_vulnerabilities ? <span className="tag warn">vuln</span> : null}
            <p>{getPackageDescriptionPreview(item.description, 220) || "No description"}</p>
          </li>
        ))}
      </ul>

      {cursor ? (
        <button type="button" onClick={loadMore} disabled={loading}>
          {loading ? "Loading..." : "Load more"}
        </button>
      ) : null}
    </section>
  );
}
