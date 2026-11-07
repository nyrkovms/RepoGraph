import { deprecationMessage, isGoPseudoVersion } from "./packageHelpers";

// `hideActive`: the list shows a status only where it is not `active`; the tags after it stay.
export function VersionStatus({ version, isGo, hideActive = false }) {
  // A Go module's author retracts a version in go.mod with a rationale; the catalog keeps it as yanked.
  const retracted = isGo && version.status === "yanked";
  // A NuGet version is unlisted by its author: hidden from search, still restorable, so it stays in the list.
  // An RPM build is superseded by a newer build of the same arch in its repository: it still exists, so it stays listed.
  const statusClass =
    version.status === "yanked" ? "tag warn" : version.status === "unlisted" || version.status === "superseded" ? "tag muted-tag" : "tag";
  const deprecation = version.deprecation ? deprecationMessage(version.deprecation) : "";
  return (
    <>
      {hideActive && version.status === "active" ? null : (
        <span
          className={statusClass}
          title={version.status === "superseded" ? "A newer build of the same arch is in the source" : undefined}
        >
          {retracted ? "retracted" : version.status}
        </span>
      )}
      {isGoPseudoVersion(version.version) ? <span className="tag cond">pseudo-version</span> : null}
      {retracted && version.status_reason ? <span className="muted version-reason">{version.status_reason}</span> : null}
      {version.deprecation ? <span className="tag warn">deprecated</span> : null}
      {deprecation ? <span className="muted version-reason">{deprecation}</span> : null}
    </>
  );
}
