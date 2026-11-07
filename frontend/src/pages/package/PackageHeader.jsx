import { installCommand } from "../../lib/installCommand";
import { CopyField } from "./CopyField";
import { ActionLink, RelocationLink } from "./Links";
import { ECOSYSTEM_LABELS } from "./packageEnv";
import { formatDate, isWebUrl, versionedPurl } from "./packageHelpers";
import { VersionStatus } from "./VersionStatus";

// A web address as a link under its label; anything else (git://, ssh) as plain text.
function LinkItem({ label, url }) {
  return (
    <li>
      {isWebUrl(url) ? (
        <a href={url} target="_blank" rel="noreferrer noopener">
          {label}
        </a>
      ) : (
        <>
          {label}: {url}
        </>
      )}
    </li>
  );
}

function Fact({ label, children }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function dependencyFact(dependencies, link) {
  if (dependencies.direct === null || dependencies.direct === undefined) {
    return "unknown";
  }
  const optional = dependencies.optional ? ` + ${dependencies.optional} optional` : "";
  return link(`${dependencies.direct}${optional}`);
}

// What a reader wants in five seconds, above every tab: which package, which version, what is wrong with it, the
// facts, how to name and install it, where it lives.
export function PackageHeader({
  title,
  ecosystem,
  pkg,
  selectedVersion,
  versions,
  loading,
  isLatest,
  versionRow,
  versionDetail,
  overview,
  deprecated,
  relocatedTo,
  descriptionParagraphs,
  onSelectVersion,
  tabHref,
  onSelectTab,
}) {
  const isGo = ecosystem === "go";
  const isMaven = ecosystem === "maven";
  const isRpm = ecosystem === "rpm";
  const tabLink = (tab, text) => (
    <ActionLink href={tabHref(tab)} onSelect={() => onSelectTab(tab)}>
      {text}
    </ActionLink>
  );
  // A version that is in the URL but not among the loaded ones still has to show in the selector.
  const options = versions.some((row) => row.version === selectedVersion) || !selectedVersion
    ? versions.map((row) => row.version)
    : [selectedVersion, ...versions.map((row) => row.version)];
  const showStatus = versionRow && (versionRow.status !== "active" || versionRow.deprecation);
  const purl = pkg?.purl && selectedVersion ? versionDetail?.purl || versionedPurl(pkg.purl, selectedVersion) : "";
  const license = pkg ? pkg.license_spdx || pkg.license_raw || "Not published by the source" : "";

  return (
    <div className="package-header">
      <h2>
        {title}
        <span className="tag eco-badge">{ECOSYSTEM_LABELS[ecosystem] || ecosystem}</span>
      </h2>
      {pkg ? (
        <>
          <div className="package-version">
            <label>
              Version
              <select
                aria-label="Version"
                value={selectedVersion}
                onChange={(event) => onSelectVersion(event.target.value)}
                disabled={loading || options.length === 0}
              >
                {options.map((version) => (
                  <option key={version} value={version}>
                    {version}
                  </option>
                ))}
              </select>
            </label>
            {isLatest ? <span className="tag">latest</span> : null}
          </div>

          {deprecated ? (
            <div className="banner warn" role="alert">
              <b>Deprecated</b>
              {deprecated !== true ? `: ${deprecated}` : ""}
              {relocatedTo ? (
                <>
                  {" "}
                  (moved to <RelocationLink alternative={relocatedTo} isMaven={isMaven} />)
                </>
              ) : null}
            </div>
          ) : null}
          {showStatus ? (
            <div className="banner warn" role="status">
              <b>Version {versionRow.version}</b> <VersionStatus version={versionRow} isGo={isGo} />
            </div>
          ) : null}
          {selectedVersion && pkg.latest_version && selectedVersion !== pkg.latest_version ? (
            <div className="banner">
              <b>Not the latest version</b>:{" "}
              <ActionLink
                href={tabHref(undefined, { version: pkg.latest_version })}
                onSelect={() => onSelectVersion(pkg.latest_version)}
              >
                go to {pkg.latest_version}
              </ActionLink>
            </div>
          ) : null}

          {descriptionParagraphs.length ? (
            <div className="package-description">
              {descriptionParagraphs.map((paragraph) => (
                <p key={paragraph}>{paragraph}</p>
              ))}
            </div>
          ) : null}

          <dl className="fact-strip">
            {formatDate(versionRow?.release_date) ? <Fact label="Released">{formatDate(versionRow.release_date)}</Fact> : null}
            <Fact label="License">{tabLink("overview", license)}</Fact>
            {overview ? (
              <>
                <Fact label="Used by">
                  {overview.used_by.direct > 0
                    ? tabLink("used-by", `${overview.used_by.direct} in catalog`)
                    : `${overview.used_by.direct} in catalog`}
                </Fact>
                <Fact label="Dependencies">{dependencyFact(overview.dependencies, (text) => tabLink("dependencies", text))}</Fact>
                <Fact label="Versions">{tabLink("versions", overview.versions.total)}</Fact>
              </>
            ) : null}
          </dl>

          {purl || selectedVersion ? (
            <div className="identifiers">
              {purl ? <CopyField label="purl" text={purl} /> : null}
              {selectedVersion && installCommand(ecosystem, pkg, selectedVersion) ? (
                <CopyField label="install" text={installCommand(ecosystem, pkg, selectedVersion)} />
              ) : null}
            </div>
          ) : null}

          {isMaven ? (
            <div className="meta-grid">
              <span>
                <b>groupId</b>: <code>{pkg.namespace}</code>
              </span>
              <span>
                <b>artifactId</b>: <code>{pkg.normalized_name}</code>
              </span>
            </div>
          ) : null}
          {isRpm ? (
            <div className="meta-grid">
              <span>
                <b>Distro</b>: <code>{pkg.namespace}</code>
              </span>
              <span>
                <b>Name</b>: <code>{pkg.normalized_name}</code>
              </span>
            </div>
          ) : null}

          {pkg.repository_url || pkg.homepage_url || pkg.documentation_url ? (
            <ul className="package-links">
              {pkg.repository_url ? <LinkItem label="Repository" url={pkg.repository_url} /> : null}
              {pkg.homepage_url ? <LinkItem label="Homepage" url={pkg.homepage_url} /> : null}
              {pkg.documentation_url ? <LinkItem label="Documentation" url={pkg.documentation_url} /> : null}
            </ul>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
