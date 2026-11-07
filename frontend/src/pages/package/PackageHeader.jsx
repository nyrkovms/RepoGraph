import { ActionLink, RelocationLink } from "./Links";
import { ECOSYSTEM_LABELS } from "./packageEnv";
import { VersionStatus } from "./VersionStatus";

// The top of the card at full width: which package, which version, what is wrong with it, what it is. The facts and the
// ways to take it are in PackageFacts, beside the tabs. `notice` is shown right under the title; for a version the
// catalog does not have (`missingVersion`) the version's own parts (status, "not the latest") are left out, as nothing
// of them exists.
export function PackageHeader({
  title,
  ecosystem,
  pkg,
  selectedVersion,
  versions,
  loading,
  isLatest,
  versionRow,
  deprecated,
  relocatedTo,
  descriptionParagraphs,
  onSelectVersion,
  tabHref,
  notice = null,
  missingVersion = false,
}) {
  const isGo = ecosystem === "go";
  const isMaven = ecosystem === "maven";
  // A version that is in the URL but not among the loaded ones still has to show in the selector.
  const options = versions.some((row) => row.version === selectedVersion) || !selectedVersion
    ? versions.map((row) => row.version)
    : [selectedVersion, ...versions.map((row) => row.version)];
  const showStatus = versionRow && (versionRow.status !== "active" || versionRow.deprecation);

  return (
    <div className="package-header">
      <div className="package-title">
        <h2>
          {title}
          <span className="tag eco-badge">{ECOSYSTEM_LABELS[ecosystem] || ecosystem}</span>
        </h2>
        {pkg ? (
          <>
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
            {isLatest ? <span className="tag">latest</span> : null}
          </>
        ) : null}
      </div>
      {notice}
      {pkg ? (
        <>
          {descriptionParagraphs.length ? (
            <div className="package-description">
              {descriptionParagraphs.map((paragraph) => (
                <p key={paragraph}>{paragraph}</p>
              ))}
            </div>
          ) : null}
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
          {showStatus && !missingVersion ? (
            <div className="banner warn" role="status">
              <b>Version {versionRow.version}</b> <VersionStatus version={versionRow} isGo={isGo} />
            </div>
          ) : null}
          {!missingVersion && selectedVersion && pkg.latest_version && selectedVersion !== pkg.latest_version ? (
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
        </>
      ) : null}
    </div>
  );
}
