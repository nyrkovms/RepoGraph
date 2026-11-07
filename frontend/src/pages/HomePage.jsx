import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { fetchCatalogSummary, fetchUpdatesSummary } from "../lib/api";
import AttentionRow from "../lib/AttentionRow";
import { ECOSYSTEM_OPTIONS, ecosystemLabel, packageHref } from "../lib/packageNames";
import { isPurl, purlDestination } from "../lib/purl";
import SearchBox from "../lib/SearchBox";
import {
  browserTimezone,
  bumpOf,
  displayName,
  ecosystemOf,
  flagsOf,
  formatDateTime,
  packageLink,
  summarizeRuns,
} from "../lib/updates";

const ATTENTION_ROWS = 3;
const UPDATED_ROWS = 5;
const DEPRECATED_ROWS = 5;

// A block's data: loading until the request answers, then the payload or the error message. The previous answer stays
// while a new one loads, so a filter change does not blank the page.
function useBlockData(load, key) {
  const [state, setState] = useState({ loading: true, data: null, error: "" });
  useEffect(() => {
    let cancelled = false;
    setState((prev) => ({ ...prev, loading: true }));
    load()
      .then((data) => {
        if (!cancelled) setState({ loading: false, data, error: "" });
      })
      .catch((err) => {
        if (!cancelled) setState({ loading: false, data: null, error: err?.message || "Request failed" });
      });
    return () => {
      cancelled = true;
    };
    // `load` closes over `key` only: the request runs once per visit and per filter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return state;
}

function BlockMessage({ state, what }) {
  if (state.loading) return <p className="muted">Loading...</p>;
  return <p className="error">Could not load {what}: {state.error}</p>;
}

function SearchBlock({ ecosystem }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");

  async function submit() {
    const q = query.trim();
    const toSearch = () => {
      const parts = [q && `q=${encodeURIComponent(q)}`, ecosystem && `ecosystem=${encodeURIComponent(ecosystem)}`].filter(Boolean);
      navigate(parts.length ? `/search?${parts.join("&")}` : "/search");
    };
    if (!q || !isPurl(q)) {
      toSearch();
      return;
    }
    // A purl names one package: go straight to its card (at the purl's version), else let the search page say that
    // nothing matches.
    try {
      const href = await purlDestination(q);
      if (href) navigate(href);
      else toSearch();
    } catch {
      toSearch();
    }
  }

  return (
    <div className="home-search">
      <p className="home-search-label">Name or purl</p>
      <div className="toolbar">
        <SearchBox
          value={query}
          onChange={setQuery}
          onSubmit={submit}
          placeholder="requests, pkg:npm/express"
          ariaLabel="Name or purl"
          ecosystem={ecosystem}
        />
        <button type="button" onClick={submit}>
          Search
        </button>
      </div>
    </div>
  );
}

// Chips narrow the page in place: the selection lives in the address as `?ecosystem=`.
function EcosystemChips({ state, ecosystem, onSelect }) {
  const ecosystems = state.data?.ecosystems;
  if (!ecosystems) return <BlockMessage state={state} what="the ecosystems" />;
  const total = ecosystems.reduce((sum, item) => sum + item.packages, 0);
  const selected = ecosystems.find((item) => item.ecosystem === ecosystem);
  return (
    <div className="home-chips">
      <ul className="chips" aria-label="Ecosystems">
        <li>
          <button type="button" className="chip" aria-pressed={!ecosystem} onClick={() => onSelect("")}>
            All <b>{total}</b>
          </button>
        </li>
        {ecosystems.map((item) => (
          <li key={item.ecosystem}>
            <button
              type="button"
              className="chip"
              aria-pressed={item.ecosystem === ecosystem}
              onClick={() => onSelect(item.ecosystem)}
            >
              {ecosystemLabel(item.ecosystem)} <b>{item.packages}</b>
            </button>
          </li>
        ))}
      </ul>
      {selected ? (
        <Link to={`/search?${new URLSearchParams({ ecosystem })}`}>
          Browse all {selected.packages} {ecosystemLabel(ecosystem)} packages
        </Link>
      ) : null}
    </div>
  );
}

// `7 of 7 sources updated · last run 06.11, 10:03`: freshness comes from the runs, not from when the data last changed.
function StatusLine({ state }) {
  if (!state.data) return <BlockMessage state={state} what="the update status" />;
  const { last_run: lastRun, last_runs: lastRuns } = state.data;
  const runs = Array.isArray(lastRuns) && lastRuns.length ? lastRuns : lastRun ? [lastRun] : [];
  if (!runs.length) return <p className="muted">No update runs yet</p>;
  const { asOf, problems } = summarizeRuns(runs);
  const updated = runs.length - problems.length;
  return (
    <p className={problems.length ? "home-status warn-text" : "home-status muted"}>
      {updated} of {runs.length} sources updated · last run {formatDateTime(asOf)}
      {problems.length ? ` · not updated: ${problems.map((p) => `${p.name} (${p.reasons.join(", ")})`).join(", ")}` : ""}
    </p>
  );
}

// An updated package without a flag: what moved, how far, and how much of the catalog depends on it.
function UpdatedRow({ item }) {
  const bump = bumpOf(item);
  const usedBy = Number(item.used_by) || 0;
  return (
    <li className="updated-row">
      <div className="update-row-head">
        <Link to={packageLink(item)}>{displayName(item)}</Link>
        <span className="tag muted-tag">{ecosystemLabel(ecosystemOf(item))}</span>
        {item.to_version ? (
          <span className="update-versions">
            {item.from_version ? <code>{item.from_version}</code> : <span className="muted">new</span>}
            {" → "}
            <code>{item.to_version}</code>
          </span>
        ) : null}
        {bump ? <span className="tag muted-tag">{bump}</span> : null}
      </div>
      {item.description ? <p className="muted row-description">{item.description}</p> : null}
      {usedBy > 0 ? (
        <p className="muted">
          used by {usedBy} {usedBy === 1 ? "package" : "packages"}
        </p>
      ) : null}
    </li>
  );
}

// The Updates page's own totals for the week, its attention rows and the five most used updated packages.
function ThisWeek({ state, ecosystem }) {
  if (!state.data) return <BlockMessage state={state} what="this week's updates" />;
  const totals = state.data.totals || {};
  // The API orders packages by severity and importance, as the Updates page shows them.
  const packages = state.data.packages || [];
  const attention = packages.filter((item) => flagsOf(item).length > 0).slice(0, ATTENTION_ROWS);
  const updated = packages
    .filter((item) => flagsOf(item).length === 0 && !item.prerelease_only)
    .slice(0, UPDATED_ROWS);
  const updatesLink = `/updates?${new URLSearchParams({ period: "week", ...(ecosystem ? { ecosystem } : {}) })}`;
  return (
    <>
      <p>
        <b>
          {totals.attention ?? 0} {totals.attention === 1 ? "needs" : "need"} attention
        </b> · {totals.packages ?? 0} packages updated · {totals.releases ?? 0}{" "}
        releases · <Link to={updatesLink}>All updates this week</Link>
      </p>
      {attention.length ? <h4>Needs attention</h4> : null}
      {attention.length ? (
        <ul className="result-list">
          {attention.map((item) => (
            <AttentionRow key={item.purl || item.name} item={item} />
          ))}
        </ul>
      ) : (
        <p className="muted">Nothing needs attention this week</p>
      )}
      {updated.length ? (
        <>
          <h4>Updated, most used first</h4>
          <ul className="result-list">
            {updated.map((item) => (
              <UpdatedRow key={item.purl || item.name} item={item} />
            ))}
          </ul>
        </>
      ) : null}
    </>
  );
}

// A row of the two lists: name, ecosystem, one clipped line of description (or the latest version), the count at the end.
function ListRow({ item, count }) {
  return (
    <li>
      <div className="home-list-main">
        <span className="home-list-head">
          <Link to={packageHref(item)}>{item.name}</Link>
          <span className="tag muted-tag">{ecosystemLabel(item.ecosystem)}</span>
        </span>
        <span className="muted row-description">
          {item.description || (item.latest_version ? `latest ${item.latest_version}` : "")}
        </span>
      </div>
      <span className="muted home-list-meta">{count}</span>
    </li>
  );
}

function MostUsed({ state }) {
  const items = state.data?.most_used;
  if (!items) return <BlockMessage state={state} what="the most used packages" />;
  return (
    <>
      {items.length ? (
        <ol className="home-list">
          {items.map((item) => (
            <ListRow key={item.purl} item={item} count={`${item.used_by} dependents`} />
          ))}
        </ol>
      ) : (
        <p className="muted">No package in the catalog is used by another one</p>
      )}
      <p className="muted home-note">Go counts test dependencies too: go.mod does not separate them.</p>
    </>
  );
}

function WorthALook({ state, ecosystem }) {
  if (!state.data) return <BlockMessage state={state} what="the packages worth a look" />;
  const deprecated = (state.data.deprecated_in_use || []).slice(0, DEPRECATED_ROWS);
  const deprecatedTotal = state.data.deprecated_in_use_total || 0;
  const stale = state.data.stale_in_use;
  const scope = ecosystem ? { ecosystem } : {};
  const staleLink = stale
    ? `/search?${new URLSearchParams({
        released_before: stale.released_before,
        min_used_by: String(stale.min_used_by),
        sort: "used_by",
        ...scope,
      })}`
    : "";
  return (
    <>
      {deprecated.length ? (
        <>
          <ul className="home-list">
            {deprecated.map((item) => (
              <ListRow key={item.purl} item={item} count={`used by ${item.used_by}`} />
            ))}
          </ul>
          {deprecatedTotal > deprecated.length ? (
            <p>
              <Link to={`/search?${new URLSearchParams({ deprecated: "true", min_used_by: "1", sort: "used_by", ...scope })}`}>
                All {deprecatedTotal} deprecated packages in use
              </Link>
            </p>
          ) : null}
        </>
      ) : null}
      {stale?.packages > 0 ? (
        <p>
          <Link to={staleLink}>
            {stale.packages} {stale.packages === 1 ? "package" : "packages"} had no release in {stale.years}+ years and{" "}
            {stale.packages === 1 ? "is" : "are"} used by {stale.min_used_by} or more
          </Link>
        </p>
      ) : null}
      {!deprecated.length && !(stale?.packages > 0) ? <p className="muted">Nothing stands out</p> : null}
    </>
  );
}

export default function HomePage() {
  const [params, setParams] = useSearchParams();
  const ecosystemParam = params.get("ecosystem") || "";
  const ecosystem = ECOSYSTEM_OPTIONS.some((option) => option.value && option.value === ecosystemParam) ? ecosystemParam : "";
  const catalog = useBlockData(() => fetchCatalogSummary({ ecosystem }), `catalog:${ecosystem}`);
  const week = useBlockData(
    () => fetchUpdatesSummary({ period: "week", tz: browserTimezone(), ...(ecosystem ? { ecosystem } : {}) }),
    `week:${ecosystem}`
  );

  function selectEcosystem(value) {
    setParams(value ? { ecosystem: value } : {});
  }

  return (
    <section className="home">
      <SearchBlock ecosystem={ecosystem} />

      <EcosystemChips state={catalog} ecosystem={ecosystem} onSelect={selectEcosystem} />

      <div className="home-status-row">
        <StatusLine state={week} />
        <p className="muted">Security advisories: PyPI only</p>
      </div>

      <h3>This week</h3>
      <ThisWeek state={week} ecosystem={ecosystem} />

      <div className="home-columns">
        <div>
          <h3>Most used in catalog</h3>
          <p className="muted home-subtitle">Packages the rest of the catalog depends on</p>
          <MostUsed state={catalog} />
        </div>
        <div>
          <h3>Worth a look</h3>
          <p className="muted home-subtitle">Deprecated, but other packages still use them</p>
          <WorthALook state={catalog} ecosystem={ecosystem} />
        </div>
      </div>
    </section>
  );
}
