import { FeaturesBlock } from "./FeaturesBlock";
import { deprecationMessage, formatDateTime, packagingOf } from "./packageHelpers";

export function SourcesTab({ pkg, name, versionDetail, selectedVersion, isGo, isCrates, isNuget, isMaven, isRpm, archesByVersion }) {
  const detail = versionDetail && versionDetail.version === selectedVersion ? versionDetail : null;
  return (
    <>
      {pkg.sources?.length ? (
        <>
          <h3>Sources</h3>
          <ul className="result-list package-sources">
            {pkg.sources.map((source) => (
              <li key={source.registry}>
                <code>{source.registry}</code>
                {source.primary ? <span className="tag">primary</span> : null}
                {!source.enabled ? <span className="tag muted-tag">disabled</span> : null}
                {source.missing_since ? <span className="tag warn">missing since {formatDateTime(source.missing_since)}</span> : null}
                {source.checked_at ? <span className="muted"> checked {formatDateTime(source.checked_at)}</span> : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {detail ? (
        <div className="version-detail">
          <b>Sources of {detail.version}</b>:
          {detail.sources?.length ? (
            <ul className="inline-list">
              {detail.sources.map((source) => (
                <li key={source.registry}>
                  {source.registry}: {source.status}
                  {!source.enabled ? <span className="tag muted-tag">disabled</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {detail.dependencies_state === "unavailable" ? <span className="tag warn">dependencies unknown</span> : null}
          {isGo && detail.status === "yanked" ? (
            <p className="muted">Retracted{detail.status_reason ? `: ${detail.status_reason}` : ""}</p>
          ) : null}
          {isRpm && archesByVersion[`${name}/${detail.version}`]?.length ? (
            <p className="muted">Arches: {archesByVersion[`${name}/${detail.version}`].join(", ")}</p>
          ) : null}
          {isRpm && detail.status === "superseded" ? (
            <p className="muted">Superseded: a newer build of the same arch is in the source, this one is still in the catalog.</p>
          ) : null}
          {isMaven && packagingOf(detail) ? (
            <p className="muted">
              Packaging: <code>{packagingOf(detail)}</code>
            </p>
          ) : null}
          {isNuget && detail.status === "unlisted" ? (
            <p className="muted">Unlisted: the author hid this version from search, it can still be restored.</p>
          ) : null}
          {detail.deprecation ? (
            <p className="muted">
              Deprecated
              {deprecationMessage(detail.deprecation) ? `: ${deprecationMessage(detail.deprecation)}` : ""}
            </p>
          ) : null}
        </div>
      ) : null}
      {isCrates && detail ? <FeaturesBlock version={detail.version} features={detail.features} /> : null}
      {!pkg.sources?.length && !detail ? <p className="muted">No source information.</p> : null}
    </>
  );
}
