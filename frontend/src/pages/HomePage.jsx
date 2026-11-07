import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { fetchCatalogSummary, fetchUpdatesSummary, searchPackages } from "../lib/api";
import { ecosystemLabel, packageHref } from "../lib/packageNames";
import SearchBox from "../lib/SearchBox";
import {
  FLAG_LABELS,
  browserTimezone,
  displayName,
  ecosystemOf,
  flagsOf,
  formatDate,
  formatDateTime,
  packageLink,
  summarizeRuns,
  usageLine,
} from "../lib/updates";

const ATTENTION_ROWS = 3;

// A block's data: loading until the request answers, then the payload or the error message.
function useBlockData(load) {
  const [state, setState] = useState({ loading: true, data: null, error: "" });
  useEffect(() => {
    let cancelled = false;
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
    // `load` is a module-level function: the request runs once per visit.
  }, [load]);
  return state;
}

const loadCatalogSummary = () => fetchCatalogSummary();
const loadWeek = () => fetchUpdatesSummary({ period: "week", tz: browserTimezone() });

function BlockMessage({ state, what }) {
  if (state.loading) return <p className="muted">Loading...</p>;
  return <p className="error">Could not load {what}: {state.error}</p>;
}

function SearchBlock() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");

  async function submit() {
    const q = query.trim();
    if (!q) {
      navigate("/search");
      return;
    }
    const toSearch = () => navigate(`/search?q=${encodeURIComponent(q)}`);
    if (q.slice(0, 4).toLowerCase() !== "pkg:") {
      toSearch();
      return;
    }
    // A purl names one package: go straight to its card, else let the search page say that nothing matches.
    try {
      const payload = await searchPackages({ q, limit: 2 });
      if (payload.data?.length === 1) navigate(packageHref(payload.data[0]));
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
        />
        <button type="button" onClick={submit}>
          Search
        </button>
      </div>
    </div>
  );
}

function EcosystemChips({ state }) {
  const ecosystems = state.data?.ecosystems;
  if (!ecosystems) return <BlockMessage state={state} what="the ecosystems" />;
  const total = ecosystems.reduce((sum, item) => sum + item.packages, 0);
  return (
    <ul className="chips" aria-label="Ecosystems">
      <li>
        <Link className="chip" to="/search">
          All <b>{total}</b>
        </Link>
      </li>
      {ecosystems.map((item) => (
        <li key={item.ecosystem}>
          <Link className="chip" to={`/search?ecosystem=${encodeURIComponent(item.ecosystem)}`}>
            {ecosystemLabel(item.ecosystem)} <b>{item.packages}</b>
          </Link>
        </li>
      ))}
    </ul>
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

function AttentionRow({ item }) {
  const flags = flagsOf(item);
  const usage = usageLine(item);
  return (
    <li>
      <div className="update-row-head">
        <Link to={packageLink(item)}>{displayName(item)}</Link>
        <span className="tag muted-tag">{ecosystemOf(item)}</span>
        {item.to_version ? (
          <span className="update-versions">
            {item.from_version ? <code>{item.from_version}</code> : <span className="muted">new</span>}
            {" → "}
            <code>{item.to_version}</code>
          </span>
        ) : null}
        {flags.map((flag) => (
          <span key={flag} className={flag === "major" ? "tag warn-major" : "tag warn"}>
            {FLAG_LABELS[flag] || flag}
          </span>
        ))}
      </div>
      {usage ? <p className="muted update-usage">{usage}</p> : null}
    </li>
  );
}

// The Updates page's own totals for the week: attention, packages and releases.
function ThisWeek({ state }) {
  if (!state.data) return <BlockMessage state={state} what="this week's updates" />;
  const totals = state.data.totals || {};
  // The API orders packages by severity and importance, as the Updates page shows them.
  const attention = (state.data.packages || []).filter((item) => flagsOf(item).length > 0).slice(0, ATTENTION_ROWS);
  return (
    <>
      <p>
        <b>{totals.attention ?? 0} need attention</b> · {totals.packages ?? 0} packages updated · {totals.releases ?? 0}{" "}
        releases · <Link to="/updates?period=week">All updates this week</Link>
      </p>
      {attention.length ? (
        <ul className="result-list">
          {attention.map((item) => (
            <AttentionRow key={item.purl || item.name} item={item} />
          ))}
        </ul>
      ) : (
        <p className="muted">Nothing needs attention this week</p>
      )}
    </>
  );
}

function ListLink({ item, children }) {
  return (
    <li>
      <Link to={packageHref(item)}>{item.name}</Link>
      <span className="muted home-list-meta">{children}</span>
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
            <ListLink key={item.purl} item={item}>
              {item.used_by}
            </ListLink>
          ))}
        </ol>
      ) : (
        <p className="muted">No package in the catalog is used by another one</p>
      )}
      <p className="muted home-note">Go counts test dependencies too: go.mod does not separate them.</p>
    </>
  );
}

function LatestReleases({ state }) {
  const items = state.data?.latest_releases;
  if (!items) return <BlockMessage state={state} what="the latest releases" />;
  if (!items.length) return <p className="muted">No dated releases yet</p>;
  return (
    <ol className="home-list">
      {items.map((item) => (
        <ListLink key={item.purl} item={item}>
          {item.latest_version} · {formatDate(item.released_at)}
        </ListLink>
      ))}
    </ol>
  );
}

function WorthALook({ state }) {
  if (!state.data) return <BlockMessage state={state} what="the packages worth a look" />;
  const deprecated = state.data.deprecated_in_use || [];
  const stale = state.data.stale_in_use;
  const staleLink = stale
    ? `/search?${new URLSearchParams({
        released_before: stale.released_before,
        min_used_by: String(stale.min_used_by),
        sort: "used_by",
      })}`
    : "";
  return (
    <>
      {deprecated.length ? (
        <>
          <h4>Deprecated, still used</h4>
          <ul className="home-list">
            {deprecated.map((item) => (
              <ListLink key={item.purl} item={item}>
                used by {item.used_by}
              </ListLink>
            ))}
          </ul>
        </>
      ) : null}
      {stale?.packages > 0 ? (
        <>
          <h4>
            No release in {stale.years}+ years, used by {stale.min_used_by}+
          </h4>
          <p>
            <Link to={staleLink}>
              {stale.packages} {stale.packages === 1 ? "package" : "packages"}
            </Link>
          </p>
        </>
      ) : null}
      {!deprecated.length && !(stale?.packages > 0) ? <p className="muted">Nothing stands out</p> : null}
    </>
  );
}

export default function HomePage() {
  const catalog = useBlockData(loadCatalogSummary);
  const week = useBlockData(loadWeek);

  return (
    <section className="home">
      <SearchBlock />

      <EcosystemChips state={catalog} />

      <div className="home-status-row">
        <StatusLine state={week} />
        <p className="muted">Security advisories: PyPI only</p>
      </div>

      <h3>This week</h3>
      <ThisWeek state={week} />

      <div className="home-columns">
        <div>
          <h3>Most used in catalog</h3>
          <MostUsed state={catalog} />
        </div>
        <div>
          <h3>Latest releases</h3>
          <LatestReleases state={catalog} />
        </div>
        <div>
          <h3>Worth a look</h3>
          <WorthALook state={catalog} />
        </div>
      </div>
    </section>
  );
}
