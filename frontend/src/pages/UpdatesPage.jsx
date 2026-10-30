import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { fetchUpdates, fetchUpdatesSummary } from "../lib/api";

const PERIODS = [
  { value: "day", label: "Today" },
  { value: "week", label: "Week" },
  { value: "custom", label: "Custom" },
];

// The native name and package page of a digest entry: from its purl (`pkg:npm/%40angular/core` -> `@angular/core`),
// so an npm package does not link to a PyPI page of the same name.
function displayName(item) {
  if (!item.purl) return item.name;
  // The scope's `@` is encoded in a purl, so a literal `@` can only start a version.
  return decodeURIComponent(item.purl.replace(/^pkg:[^/]+\//, "").replace(/@[^/]*$/, ""));
}

// The slug of an ecosystem differs from its purl type for crates (`pkg:cargo/...`).
const PURL_TYPE_ECOSYSTEMS = { cargo: "crates" };

function ecosystemOf(item) {
  if (item.ecosystem) return item.ecosystem;
  const type = /^pkg:([^/]+)\//.exec(item.purl || "")?.[1];
  return PURL_TYPE_ECOSYSTEMS[type] || type || "pypi";
}

function packageLink(item) {
  const ecosystem = ecosystemOf(item);
  return `/package/${ecosystem}/${displayName(item).split("/").map(encodeURIComponent).join("/")}`;
}

const TOTAL_LABELS = [
  ["packages", "Packages"],
  ["releases", "Releases"],
  ["yanked", "Yanked"],
  ["removed", "Removed"],
  ["vulnerabilities", "Vulnerabilities"],
  ["unavailable", "Unavailable"],
];

const FLAG_LABELS = {
  unavailable: "unavailable",
  vulnerability_added: "new vulnerability",
  removed: "removed",
  yanked: "yanked",
  deprecated: "deprecated",
  major: "major",
};

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

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function formatDateTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

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

function bumpOf(item) {
  return item.max_bump && item.max_bump !== "other" ? item.max_bump : null;
}

function flagsOf(item) {
  const flags = Array.isArray(item.flags) ? [...item.flags] : [];
  if (item.max_bump === "major" && !flags.includes("major")) {
    flags.push("major");
  }
  return flags;
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

function RunStatus({ lastRun }) {
  if (!lastRun) {
    return <p className="muted">Data as of: no runs yet</p>;
  }
  const asOf = lastRun.finished_at || lastRun.started_at;
  const failed = lastRun.packages_failed || 0;
  const problem = lastRun.status !== "success" || failed > 0;
  return (
    <p className={problem ? "update-run warn-text" : "update-run muted"}>
      Data as of {formatDateTime(asOf)}
      <span className={problem ? "tag warn" : "tag cond"}>{lastRun.status}</span>
      {failed > 0 ? <span className="tag warn">{failed} failed</span> : null}
      {problem ? <span> Last run was not fully successful, data may be incomplete.</span> : null}
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

  const [period, setPeriod] = useState("day");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
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
  }, [period, customSince, customUntil, waitingForRange]);

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

  const packages = summary?.packages || [];
  const totals = summary?.totals;

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
              <input type="date" value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} />
            </label>
            <label>
              To
              <input type="date" value={customTo} onChange={(e) => setCustomTo(e.target.value)} />
            </label>
          </>
        ) : null}
      </div>

      {waitingForRange ? <p className="muted">Pick a valid date range (from not later than to).</p> : null}
      {loading ? <p className="muted">Loading...</p> : null}
      {error ? <p className="error">{error}</p> : null}

      {summary && !waitingForRange ? (
        <>
          <RunStatus lastRun={summary.last_run} />

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

          {!packages.length && !loading ? <p className="muted">No updates in this period.</p> : null}

          <ul className="result-list">
            {packages.map((item) => {
              // purl: with several ecosystems two entries can share a name.
              const rowKey = item.purl || item.name;
              const isOpen = Boolean(expanded[rowKey]);
              const bump = bumpOf(item);
              const flags = flagsOf(item);
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
                    {item.to_version ? (
                      <span className="update-versions">
                        {item.from_version ? <code>{item.from_version}</code> : <span className="muted">new</span>}
                        {" → "}
                        <code>{item.to_version}</code>
                      </span>
                    ) : null}
                    {bump && !flags.includes(bump) ? <span className="tag muted-tag">{bump}</span> : null}
                    {flags.map((flag) => (
                      <span key={flag} className={flag === "major" ? "tag warn-major" : "tag warn"}>
                        {FLAG_LABELS[flag] || flag}
                      </span>
                    ))}
                    {item.last_detected_at ? (
                      <span className="muted update-detected">{formatDateTime(item.last_detected_at)}</span>
                    ) : null}
                  </div>
                  <CompactList label="Releases" items={item.releases} />
                  <CompactList label="Yanked" items={item.yanked} warn />
                  <CompactList label="Removed" items={item.removed} warn />
                  <CompactList label="Vulnerabilities" items={item.vulnerabilities} warn />
                  {isOpen ? (
                    <EventTimeline state={events[rowKey]} onLoadMore={() => loadEvents(rowKey, events[rowKey]?.cursor)} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
    </section>
  );
}
