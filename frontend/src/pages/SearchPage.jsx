import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { fetchPackages, searchPackages } from "../lib/api";
import { getPackageDescriptionPreview } from "../lib/packageDescription";

const AUTOCOMPLETE_LIMIT = 8;

const ECOSYSTEM_OPTIONS = [
  { value: "", label: "All" },
  { value: "pypi", label: "PyPI" },
  { value: "npm", label: "npm" },
  { value: "crates", label: "crates.io" },
  { value: "go", label: "Go" },
];

function ecosystemLabel(ecosystem) {
  return ECOSYSTEM_OPTIONS.find((option) => option.value === ecosystem)?.label || ecosystem;
}

// The native spelling of the package name: an npm scope is kept apart from the name in the catalog (`@angular` + `core`).
function nativeName(item) {
  return item.namespace ? `@${item.namespace}/${item.normalized_name}` : item.normalized_name;
}

function displayName(item) {
  return item.namespace ? `@${item.namespace}/${item.name}` : item.name;
}

function packageHref(item) {
  return `/package/${item.ecosystem}/${nativeName(item)}`;
}

function itemKey(item) {
  return `${item.ecosystem}-${nativeName(item)}-${item.latest_version}`;
}

export default function SearchPage() {
  const navigate = useNavigate();
  const autocompleteRequestId = useRef(0);

  const [query, setQuery] = useState("");
  const [ecosystem, setEcosystem] = useState("");
  const [rows, setRows] = useState([]);
  const [cursor, setCursor] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);

  // The list is reloaded on mount and whenever the ecosystem switcher changes.
  useEffect(() => {
    loadFirstPage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ecosystem]);

  useEffect(() => {
    const trimmedQuery = query.trim();
    if (!trimmedQuery) {
      setSuggestions([]);
      setSuggestionsOpen(false);
      setHighlightedIndex(-1);
      return;
    }

    const requestId = autocompleteRequestId.current + 1;
    autocompleteRequestId.current = requestId;

    const timeoutId = window.setTimeout(async () => {
      try {
        const payload = await searchPackages({ q: trimmedQuery, limit: AUTOCOMPLETE_LIMIT, sort: "name", ...ecosystemFilter() });
        if (autocompleteRequestId.current !== requestId) {
          return;
        }
        setSuggestions(payload.data || []);
        setSuggestionsOpen(Boolean(payload.data?.length));
        setHighlightedIndex((payload.data || []).length ? 0 : -1);
      } catch {
        if (autocompleteRequestId.current !== requestId) {
          return;
        }
        setSuggestions([]);
        setSuggestionsOpen(false);
        setHighlightedIndex(-1);
      }
    }, 250);

    return () => window.clearTimeout(timeoutId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, ecosystem]);

  function ecosystemFilter() {
    return ecosystem ? { ecosystem } : {};
  }

  async function loadFirstPage() {
    setLoading(true);
    setError("");
    try {
      const payload = query.trim()
        ? await searchPackages({ q: query.trim(), limit: 20, sort: "name", ...ecosystemFilter() })
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
        ? await searchPackages({ q: query.trim(), limit: 20, sort: "name", ...ecosystemFilter(), cursor })
        : await fetchPackages({ limit: 20, sort: "name", ...ecosystemFilter(), cursor });
      setRows((prev) => [...prev, ...(payload.data || [])]);
      setCursor(payload.cursor || null);
    } catch (err) {
      setError(err?.message || "Pagination request failed");
    } finally {
      setLoading(false);
    }
  }

  function closeSuggestions() {
    setSuggestionsOpen(false);
    setHighlightedIndex(-1);
  }

  function selectSuggestion(item) {
    setQuery(item.normalized_name);
    closeSuggestions();
    navigate(packageHref(item));
  }

  function handleSubmit() {
    closeSuggestions();
    loadFirstPage();
  }

  function handleInputKeyDown(event) {
    if (!suggestionsOpen || !suggestions.length) {
      if (event.key === "Enter") {
        event.preventDefault();
        handleSubmit();
      }
      return;
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlightedIndex((current) => (current + 1) % suggestions.length);
      return;
    }

    if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlightedIndex((current) => (current <= 0 ? suggestions.length - 1 : current - 1));
      return;
    }

    if (event.key === "Escape") {
      event.preventDefault();
      closeSuggestions();
      return;
    }

    if (event.key === "Enter") {
      event.preventDefault();
      if (highlightedIndex >= 0 && highlightedIndex < suggestions.length) {
        selectSuggestion(suggestions[highlightedIndex]);
      } else {
        handleSubmit();
      }
    }
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
        <div className="autocomplete">
          <input
            placeholder="flask, requests, numpy..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleInputKeyDown}
            onFocus={() => {
              if (suggestions.length) {
                setSuggestionsOpen(true);
              }
            }}
            onBlur={() => {
              window.setTimeout(() => closeSuggestions(), 120);
            }}
            aria-label="Search query"
            autoComplete="off"
          />
          {suggestionsOpen && suggestions.length ? (
            <ul className="autocomplete-list" role="listbox" aria-label="Search suggestions">
              {suggestions.map((item, index) => (
                <li key={itemKey(item)}>
                  <button
                    type="button"
                    className={index === highlightedIndex ? "autocomplete-item active" : "autocomplete-item"}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectSuggestion(item)}
                  >
                    <span>
                      <b>{displayName(item)}</b>
                      <span className="tag eco-badge">{ecosystemLabel(item.ecosystem)}</span>
                      <span className="muted autocomplete-version">{item.latest_version}</span>
                    </span>
                    {item.description ? (
                      <span className="muted autocomplete-description">{getPackageDescriptionPreview(item.description, 120)}</span>
                    ) : null}
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
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
