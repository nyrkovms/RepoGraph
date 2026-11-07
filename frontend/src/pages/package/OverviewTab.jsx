import { ActionLink } from "./Links";
import { RPM_NOTE } from "./UsedByTab";
import { emptyVulnerabilitiesText, formatDate, monthLabel, plural } from "./packageHelpers";

const LINES_SHOWN = 3;
const LICENSES_SHOWN = 6;

function Block({ title, tab, tabLabel, tabHref, onSelectTab, children }) {
  return (
    <section className="overview-block">
      <h3>{title}</h3>
      {children}
      {tab ? (
        <p className="overview-more">
          <ActionLink href={tabHref(tab)} onSelect={() => onSelectTab(tab)}>
            {tabLabel}
          </ActionLink>
        </p>
      ) : null}
    </section>
  );
}

// A row of the "direct / indirect" pairs: the bar is drawn in CSS, its length is the share of the larger value.
function BarRow({ label, value, max }) {
  const share = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div className="bar-row">
      <span className="bar-label">{label}</span>
      <span className="bar-track" aria-hidden="true">
        <span className="bar-fill" style={{ width: `${share}%` }} />
      </span>
      <span className="bar-value">{value}</span>
    </div>
  );
}

function SecurityBlock({ vulns, coverage, isLatest }) {
  return (
    <Block title="Security">
      {vulns.length === 0 ? <p>{emptyVulnerabilitiesText(coverage, isLatest)}</p> : null}
      {vulns.length > 0 && !isLatest ? <p className="muted">Listed for the package, not for this version</p> : null}
      {vulns.length > 0 ? (
        <ul className="result-list">
          {vulns.map((vuln) => (
            <li key={vuln.source_id}>
              <b>{vuln.source_id}</b>
              {vuln.severity ? <span className="tag warn">{vuln.severity}</span> : null}
              <p>{vuln.summary}</p>
            </li>
          ))}
        </ul>
      ) : null}
    </Block>
  );
}

function LicensesBlock({ license, overview, isRpm }) {
  const dependencies = overview.dependencies;
  const licenses = overview.licenses || [];
  let text;
  if (isRpm && dependencies.direct !== null) {
    text = null;   // the licenses of what a capability leads to are not counted
  } else if (dependencies.direct === null) {
    text = "Dependencies unknown";
  } else if (dependencies.direct === 0) {
    text = "No dependencies";
  } else if (licenses.length === 0) {
    text = "No dependency in this catalog has a license on record";
  } else {
    const shown = licenses.slice(0, LICENSES_SHOWN).map((item) => `${item.license} ${item.count}`);
    const rest = licenses.length - LICENSES_SHOWN;
    text = `Dependencies: ${shown.join(" · ")}${rest > 0 ? ` · ${rest} more` : ""}`;
  }
  return (
    <Block title="Licenses">
      <p>
        <b>This package</b>: {license}
      </p>
      {text ? <p>{text}</p> : null}
    </Block>
  );
}

function DependenciesBlock({ dependencies, tabHref, onSelectTab }) {
  const { direct, indirect, optional, not_in_catalog: missing } = dependencies;
  const max = Math.max(direct || 0, indirect || 0);
  return (
    <Block title="Dependencies" tab="dependencies" tabLabel="Open dependencies" tabHref={tabHref} onSelectTab={onSelectTab}>
      {direct === null ? (
        <p>Dependencies unknown: the source has no metadata for this version</p>
      ) : (
        <>
          <BarRow label="Direct" value={direct} max={max} />
          {indirect !== null ? <BarRow label="Indirect" value={indirect} max={max} /> : null}
          {optional > 0 ? <p>Plus {optional} optional</p> : null}
          {missing > 0 ? <p>{missing} not in catalog</p> : null}
          {direct > 0 && indirect !== null ? <p className="muted">Indirect counts only packages in this catalog: a lower bound.</p> : null}
        </>
      )}
    </Block>
  );
}

function UsedByBlock({ usedBy, tabHref, onSelectTab }) {
  const rpm = usedBy.indirect === null;
  const max = Math.max(usedBy.direct, usedBy.indirect || 0);
  const used = usedBy.direct + (usedBy.indirect || 0) > 0;
  return (
    <Block title="Used by, in this catalog" tab={used ? "used-by" : undefined} tabLabel="Open used by" tabHref={tabHref} onSelectTab={onSelectTab}>
      {!used ? (
        <p>{rpm ? RPM_NOTE : "No packages in this catalog depend on it"}</p>
      ) : (
        <>
          <BarRow label="Direct" value={usedBy.direct} max={max} />
          {rpm ? <p className="muted">{RPM_NOTE}</p> : <BarRow label="Indirect" value={usedBy.indirect} max={max} />}
        </>
      )}
    </Block>
  );
}

function ActivityBlock({ activity, tabHref, onSelectTab }) {
  const total = activity.reduce((sum, item) => sum + item.releases, 0);
  const max = Math.max(...activity.map((item) => item.releases), 1);
  return (
    <Block title="Release activity" tab="versions" tabLabel="Open versions" tabHref={tabHref} onSelectTab={onSelectTab}>
      <ol className="activity" aria-label="Releases per month">
        {activity.map((item) => (
          <li key={item.month} aria-label={`${monthLabel(item.month)}: ${plural(item.releases, "release")}`}>
            <span className="activity-bar" style={{ height: `${Math.round((item.releases / max) * 100)}%` }} aria-hidden="true" />
          </li>
        ))}
      </ol>
      <p className="muted activity-axis">
        <span>{activity.length ? monthLabel(activity[0].month) : ""}</span>
        <span>{activity.length ? monthLabel(activity[activity.length - 1].month) : ""}</span>
      </p>
      <p>{plural(total, "release")} in 12 months, prereleases included</p>
    </Block>
  );
}

function LinesBlock({ lines, tabHref, onSelectTab }) {
  return (
    <Block title="Release lines" tab="versions" tabLabel="Open versions" tabHref={tabHref} onSelectTab={onSelectTab}>
      <ul className="line-list">
        {lines.slice(0, LINES_SHOWN).map((line) => (
          <li key={line.line}>
            <b>{line.line}</b> <span>{line.latest}</span> <span className="muted">{formatDate(line.released_at)}</span>
          </li>
        ))}
      </ul>
    </Block>
  );
}

export function OverviewTab({ overview, overviewState, pkg, ecosystem, vulns, vulnCoverage, isLatest, tabHref, onSelectTab }) {
  const license = pkg.license_spdx || pkg.license_raw || "Not published by the source";
  return (
    <div className="overview-grid">
      <SecurityBlock vulns={vulns} coverage={vulnCoverage} isLatest={isLatest} />
      {overview ? (
        <>
          <LicensesBlock license={license} overview={overview} isRpm={ecosystem === "rpm"} />
          <DependenciesBlock dependencies={overview.dependencies} tabHref={tabHref} onSelectTab={onSelectTab} />
          <UsedByBlock usedBy={overview.used_by} tabHref={tabHref} onSelectTab={onSelectTab} />
          <ActivityBlock activity={overview.activity} tabHref={tabHref} onSelectTab={onSelectTab} />
          {ecosystem !== "rpm" && overview.lines.length ? (
            <LinesBlock lines={overview.lines} tabHref={tabHref} onSelectTab={onSelectTab} />
          ) : null}
        </>
      ) : (
        <p className="muted overview-state">{overviewState === "error" ? "Overview is not available" : "Loading overview..."}</p>
      )}
    </div>
  );
}
