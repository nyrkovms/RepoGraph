import { VERSIONS_SHOWN } from "./packageEnv";
import { formatDate, packagingOf } from "./packageHelpers";
import { VersionStatus } from "./VersionStatus";

export function VersionsTab({ versions, name, isGo, isMaven, isRpm, archesByVersion }) {
  return (
    <>
      <h3>Versions</h3>
      <ul className="inline-list">
        {versions.slice(0, VERSIONS_SHOWN).map((v) => (
          <li key={v.version}>
            {v.version}
            <VersionStatus version={v} isGo={isGo} />
            {isMaven && packagingOf(v) ? <span className="tag cond">{packagingOf(v)}</span> : null}
            {isRpm
              ? (archesByVersion[`${name}/${v.version}`] || []).map((item) => (
                  <span key={item} className="tag cond">
                    {item}
                  </span>
                ))
              : null}
            {formatDate(v.release_date) ? <span className="muted version-date">{formatDate(v.release_date)}</span> : null}
          </li>
        ))}
      </ul>
    </>
  );
}
