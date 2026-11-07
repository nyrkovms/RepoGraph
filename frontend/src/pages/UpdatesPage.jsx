import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { fetchUpdates, fetchUpdatesSummary } from "../lib/api";
import {
  FLAG_LABELS,
  browserTimezone,
  bumpOf,
  compareLink,
  displayName,
  ecosystemOf,
  flagsOf,
  formatDate,
  formatDateTime,
  packageLink,
  summarizeRuns,
  usageLine,
} from "../lib/updates";

const PERIODS = [
  { value: "day", label: "Today" },
  { value: "week", label: "Week" },
  { value: "custom", label: "Custom" },
];

const ECOSYSTEMS = ["pypi", "npm", "maven", "nuget", "go", "crates", "rpm"];

// The "Type" filter: what the analyst picks, and the event kinds the API gets for it.
const TYPE_FILTERS = [
  { value: "releases", label: "Releases", kinds: ["release_added"] },
  { value: "yanked", label: "Yanked", kinds: ["release_yanked"] },
  { value: "removed", label: "Removed", kinds: ["release_removed"] },
  { value: "deprecated", label: "Deprecated", kinds: ["release_deprecated"] },
  { value: "vulnerabilities", label: "Vulnerabilities", kinds: ["vulnerability_added"] },
  { value: "unavailable", label: "Unavailable", kinds: ["project_unavailable"] },
  { value: "sources", label: "Sources added", kinds: ["source_added"] },
];

const TOTAL_LABELS = [
  ["attention", "Attention"],
  ["packages", "Packages"],
  ["releases", "Releases"],
  ["yanked", "Yanked"],
  ["removed", "Removed"],
  ["vulnerabilities", "Vulnerabilities"],
  ["unavailable", "Unavailable"],
];

const KIND_LABELS = {
  tracking_started: "Tracking started",
  release_added: "Release added",
  release_yanked: "Release yanked",
  release_unyanked: "Release unyanked",
  release_removed: "Release removed",
  release_restored: "Release restored",
  vulnerability_added: "Vulnerability added",
  vulnerability_withdrawn: "Vulnerability withdrawn",
  project_unavailable: "Project unavailable",
  project_restored: "Project restored",
  release_deprecated: "Release deprecated",
  source_added: "Source added",
};

const WARN_KINDS = new Set(["release_yanked", "release_removed", "vulnerability_added", "project_unavailable"]);

const EVENTS_PAGE_SIZE = 50;
const LIST_PREVIEW_LIMIT = 8;

function parseDateInput(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) {
    return null;
  }
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

// Custom range: [start of "from" day, start of the day after "to") in the browser's local timezone.
function buildCustomRange(fromValue, toValue) {
  const start = parseDateInput(fromValue);
  const end = parseDateInput(toValue);
  if (!start || !end || end < start) {
    return null;
  }
  end.setDate(end.getDate() + 1);
  return { since: start.toISOString(), until: end.toISOString() };
}

function CompactList({ label, items, warn = false }) {
  if (!Array.isArray(items) || !items.length) {
    return null;
  }
  const shown = items.slice(0, LIST_PREVIEW_LIMIT);
  const hidden = items.length - shown.length;
  return (
    <p className="muted update-list">
      <span className={warn ? "update-list-label warn-text" : "update-list-label"}>{label}:</span> {shown.join(", ")}
      {hidden > 0 ? ` +${hidden} more` : ""}
    </p>
  );
}

function RunStatus({ lastRun, lastRuns }) {
  const runs = Array.isArray(lastRuns) && lastRuns.length ? lastRuns : lastRun ? [lastRun] : [];
  if (!runs.length) {
    return <p className="muted">Data as of: no runs yet</p>;
  }
  const { asOf, problems } = summarizeRuns(runs);
  const problem = problems.length > 0;
  return (
    <p className={problem ? "update-run warn-text" : "update-run muted"}>
      Data as of {formatDateTime(asOf)}
      {problem ? null : <span className="tag cond">success</span>}
      {problem ? (
        <span> Not fully updated: {problems.map((p) => `${p.name} (${p.reasons.join(", ")})`).join(", ")}</span>
      ) : null}
    </p>
  );
}

function EventTimeline({ state, onLoadMore }) {
  if (!state || (state.loading && !state.events.length)) {
    return <p className="muted">Loading events...</p>;
  }
  return (
    <div className="update-events">
      {state.error ? <p className="error">{state.error}</p> : null}
      {!state.error && !state.events.length ? <p className="muted">No events in this period.</p> : null}
      {state.events.length ? (
        <ol className="timeline">
          {state.events.map((event) => (
            <li key={event.id} className="timeline-item">
              <span className={WARN_KINDS.has(event.kind) ? "tag warn timeline-kind" : "tag timeline-kind"}>
                {KIND_LABELS[event.kind] || event.kind}
              </span>
              {event.version ? <code>{event.version}</code> : null}
              {event.details?.previous_version ? (
                <span className="muted"> (from {event.details.previous_version})</span>
              ) : null}
              <span className="muted timeline-time">
                detected {formatDateTime(event.detected_at)}
                {event.occurred_at ? `, occurred ${formatDateTime(event.occurred_at)}` : ""}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
      {state.cursor ? (
        <button type="button" onClick={onLoadMore} disabled={state.loading}>
          {state.loading ? "Loading..." : "Load more events"}
        </button>
      ) : null}
    </div>
  );
}

export default function UpdatesPage() {
  const requestId = useRef(0);
  const eventsRequestIds = useRef({});

  // The view lives in the URL so a link can be shared; a default adds nothing to it.
  const [searchParams, setSearchParams] = useSearchParams();
  const periodParam = searchParams.get("period");
  const period = PERIODS.some((item) => item.value === periodParam) ? periodParam : "day";
  const customFrom = searchParams.get("from") || "";
  const customTo = searchParams.get("to") || "";
  const ecosystemParam = searchParams.get("ecosystem");
  const ecosystem = ECOSYSTEMS.includes(ecosystemParam) ? ecosystemParam : "";
  const typeParam = searchParams.get("type");
  const typeFilter = TYPE_FILTERS.find((item) => item.value === typeParam) || null;
  const type = typeFilter ? typeFilter.value : "";
  const showPrereleases = searchParams.get("prereleases") === "1";

  function setParam(key, value) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true }
    );
  }

  function setPeriod(value) {
    setParam("period", value === "day" ? "" : value);
  }
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState({});
  const [events, setEvents] = useState({});

  const customRange = period === "custom" ? buildCustomRange(customFrom, customTo) : null;
  const customSince = customRange?.since;
  const customUntil = customRange?.until;
  const waitingForRange = period === "custom" && !customRange;

  useEffect(() => {
    if (waitingForRange) {
      return;
    }
    const current = requestId.current + 1;
    requestId.current = current;
    const params =
      period === "custom"
        ? { since: customSince, until: customUntil, tz: browserTimezone() }
        : { period, tz: browserTimezone() };
    if (ecosystem) params.ecosystem = ecosystem;
    const kinds = TYPE_FILTERS.find((item) => item.value === type)?.kinds;
    if (kinds) params.kind = kinds;

    setLoading(true);
    setError("");
    setExpanded({});
    setEvents({});
    fetchUpdatesSummary(params)
      .then((payload) => {
        if (requestId.current === current) {
          setSummary(payload);
        }
      })
      .catch((err) => {
        if (requestId.current === current) {
          setSummary(null);
          setError(err?.message || "Updates request failed");
        }
      })
      .finally(() => {
        if (requestId.current === current) {
          setLoading(false);
        }
      });
  }, [period, customSince, customUntil, waitingForRange, ecosystem, type]);

  // `name` is the row key: the purl (the API takes it as `package`), or the bare name of an older API.
  async function loadEvents(name, cursor = null) {
    const current = (eventsRequestIds.current[name] || 0) + 1;
    eventsRequestIds.current[name] = current;
    setEvents((prev) => ({
      ...prev,
      [name]: { events: prev[name]?.events || [], cursor: null, loading: true, error: "" },
    }));
    try {
      const payload = await fetchUpdates({
        package: name,
        since: summary?.since,
        until: summary?.until,
        limit: EVENTS_PAGE_SIZE,
        cursor,
      });
      if (eventsRequestIds.current[name] !== current) {
        return;
      }
      setEvents((prev) => ({
        ...prev,
        [name]: {
          events: [...(cursor ? prev[name]?.events || [] : []), ...(payload.data || [])],
          cursor: payload.has_more ? payload.cursor || null : null,
          loading: false,
          error: "",
        },
      }));
    } catch (err) {
      if (eventsRequestIds.current[name] !== current) {
        return;
      }
      setEvents((prev) => ({
        ...prev,
        [name]: {
          events: prev[name]?.events || [],
          cursor: null,
          loading: false,
          error: err?.message || "Events request failed",
        },
      }));
    }
  }

  function toggleRow(name) {
    const open = !expanded[name];
    setExpanded((prev) => ({ ...prev, [name]: open }));
    if (open && !events[name]) {
      loadEvents(name);
    }
  }

  const filtered = Boolean(ecosystem || type);
  const totals = summary?.totals;
  const hiddenPrereleases = totals?.prereleases || 0;
  // A prerelease-only package is hidden unless it carries a flag: a flag is never hidden.
  const packages = (summary?.packages || []).filter(
    (item) => showPrereleases || !item.prerelease_only || flagsOf(item).length > 0
  );

  // The API sorts by severity and importance: each section keeps that order.
  const attention = packages.filter((item) => flagsOf(item).length > 0);
  const updated = packages.filter((item) => flagsOf(item).length === 0);

  function renderRow(item) {
    // purl: with several ecosystems two entries can share a name.
    const rowKey = item.purl || item.name;
    const isOpen = Boolean(expanded[rowKey]);
    const bump = bumpOf(item);
    const flags = flagsOf(item);
    const usage = usageLine(item);
    return (
      <li key={rowKey}>
        <div className="update-row-head">
          <button
            type="button"
            className="tree-toggle"
            aria-expanded={isOpen}
            aria-label={`${isOpen ? "Hide" : "Show"} events for ${item.name}`}
            onClick={() => toggleRow(rowKey)}
          >
            {isOpen ? "-" : "+"}
          </button>
          <Link to={packageLink(item)}>{displayName(item)}</Link>
          <span className="tag muted-tag">{ecosystemOf(item)}</span>
          {item.to_version ? (
            <span className="update-versions">
              {item.from_version ? <code>{item.from_version}</code> : <span className="muted">new</span>}
              {" → "}
              <code>{item.to_version}</code>
            </span>
          ) : null}
          {item.from_version && item.to_version ? (
            <Link to={compareLink(item)}>Compare</Link>
          ) : null}
          {bump && !flags.includes(bump) ? <span className="tag muted-tag">{bump}</span> : null}
          {flags.map((flag) => (
            <span key={flag} className={flag === "major" ? "tag warn-major" : "tag warn"}>
              {FLAG_LABELS[flag] || flag}
            </span>
          ))}
          {item.released_at ? (
            <span className="muted update-detected">released {formatDate(item.released_at)}</span>
          ) : item.last_detected_at ? (
            <span className="muted update-detected">detected {formatDateTime(item.last_detected_at)}</span>
          ) : null}
        </div>
        {item.description ? <p className="muted update-description">{item.description}</p> : null}
        {usage ? <p className="muted update-usage">{usage}</p> : null}
        <CompactList label="Releases" items={item.releases} />
        <CompactList label="Yanked" items={item.yanked} warn />
        <CompactList label="Removed" items={item.removed} warn />
        <CompactList label="Vulnerabilities" items={item.vulnerabilities} warn />
        {isOpen ? (
          <EventTimeline state={events[rowKey]} onLoadMore={() => loadEvents(rowKey, events[rowKey]?.cursor)} />
        ) : null}
      </li>
    );
  }

  return (
    <section>
      <h2>Updates</h2>

      <div className="toolbar" role="group" aria-label="Period">
        {PERIODS.map((item) => (
          <button
            key={item.value}
            type="button"
            className={period === item.value ? "period-button active" : "period-button"}
            aria-pressed={period === item.value}
            onClick={() => setPeriod(item.value)}
          >
            {item.label}
          </button>
        ))}
        {period === "custom" ? (
          <>
            <label>
              From
              <input type="date" value={customFrom} onChange={(e) => setParam("from", e.target.value)} />
            </label>
            <label>
              To
              <input type="date" value={customTo} onChange={(e) => setParam("to", e.target.value)} />
            </label>
          </>
        ) : null}
      </div>

      <div className="toolbar" role="group" aria-label="Filters">
        <label>
          Ecosystem
          <select value={ecosystem} onChange={(e) => setParam("ecosystem", e.target.value)}>
            <option value="">All</option>
            {ECOSYSTEMS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          Type
          <select value={type} onChange={(e) => setParam("type", e.target.value)}>
            <option value="">All</option>
            {TYPE_FILTERS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        {hiddenPrereleases > 0 ? (
          <label className="check-label">
            <input
              type="checkbox"
              checked={showPrereleases}
              onChange={(e) => setParam("prereleases", e.target.checked ? "1" : "")}
            />
            Show prereleases ({hiddenPrereleases})
          </label>
        ) : null}
      </div>

      {waitingForRange ? <p className="muted">Pick a valid date range (from not later than to).</p> : null}
      {loading ? <p className="muted">Loading...</p> : null}
      {error ? <p className="error">{error}</p> : null}

      {summary && !waitingForRange ? (
        <>
          <RunStatus lastRun={summary.last_run} lastRuns={summary.last_runs} />

          {totals ? (
            <dl className="totals" aria-label="Totals">
              {TOTAL_LABELS.map(([key, label]) => (
                <div key={key} className="totals-item">
                  <dt className="muted">{label}</dt>
                  <dd>{totals[key] ?? 0}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {!packages.length && !loading ? (
            <p className="muted">{filtered ? "No updates match these filters." : "No updates in this period."}</p>
          ) : null}

          {packages.length ? (
            <>
              <h3>Needs attention ({attention.length})</h3>
              {attention.length ? (
                <ul className="result-list">{attention.map(renderRow)}</ul>
              ) : (
                <p className="muted">Nothing needs attention</p>
              )}
              <h3>Updated ({updated.length})</h3>
              <ul className="result-list">{updated.map(renderRow)}</ul>
            </>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
