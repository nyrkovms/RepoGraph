import { Link } from "react-router-dom";

import { displayName, packageHref } from "../../lib/packageNames";
import { ActionLink } from "./Links";

const NOTE =
  "Counted among latest versions in this catalog: runtime, peer and provided dependencies. It shows what is affected here, not how popular the package is.";

// RPM: a dependency names a capability, and the catalog does not resolve which package provides it.
export const RPM_NOTE = "Dependencies on capabilities are not counted";

const dash = (items) => (items.length ? items.join(", ") : "—");

// What an empty list says: only a catalog with no dependents at all may say nobody depends on the package.
function emptyText(relation, totals) {
  if (!totals || totals.direct + totals.indirect === 0 || !relation) {
    return "No packages in this catalog depend on it";
  }
  return `No ${relation} dependents in this catalog`;
}

function Chips({ relation, totals, tabHref, onSelectRelation }) {
  const chips = [
    { value: "", label: "All", count: totals.direct + totals.indirect },
    { value: "direct", label: "Direct", count: totals.direct },
    { value: "indirect", label: "Indirect", count: totals.indirect },
  ];
  return (
    <nav className="chips" aria-label="Relation">
      {chips.map((chip) => (
        <ActionLink
          key={chip.value}
          href={tabHref("used-by", { relation: chip.value || null })}
          onSelect={() => onSelectRelation(chip.value)}
          className={`chip${chip.value === relation ? " active" : ""}`}
          aria-current={chip.value === relation ? "true" : undefined}
        >
          {`${chip.label} ${chip.count}`}
        </ActionLink>
      ))}
    </nav>
  );
}

// The catalog packages that break if this one changes (SPEC-CATALOG-PAGES.md §5): `list` comes from useDependents.
export function UsedByTab({ list, totals, relation, tabHref, onSelectRelation, isRpm = false }) {
  const known = list.totals || totals;
  const empty = list.state === "ready" && list.rows.length === 0;
  return (
    <>
      <h3>Used by</h3>
      {isRpm ? <p className={empty ? undefined : "muted"}>{RPM_NOTE}</p> : null}
      {known && !isRpm ? <Chips relation={relation} totals={known} tabHref={tabHref} onSelectRelation={onSelectRelation} /> : null}
      {list.state === "loading" ? <p className="muted">Loading...</p> : null}
      {list.state === "error" ? <p className="error">Used by is not available</p> : null}
      {empty && !isRpm ? <p>{emptyText(relation, known)}</p> : null}
      {list.rows.length > 0 ? (
        <div className="table-wrap">
          <table className="used-by-table">
            <thead>
              <tr>
                <th>Package</th>
                <th>Version</th>
                <th>Requires</th>
                <th>Type</th>
                <th>Relation</th>
              </tr>
            </thead>
            <tbody>
              {list.rows.map((row) => (
                <tr key={row.purl}>
                  <td>
                    <Link to={packageHref(row)}>{displayName(row)}</Link>
                  </td>
                  <td>{row.latest_version || "—"}</td>
                  <td>{row.relation === "direct" ? dash(row.requires) : "—"}</td>
                  <td>{row.relation === "direct" ? dash(row.dependency_types) : "—"}</td>
                  <td>{row.relation === "direct" ? "Direct" : "Indirect"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {list.hasMore ? (
        <p>
          <button type="button" onClick={list.loadMore} disabled={list.loadingMore}>
            {list.loadingMore ? "Loading..." : "Load more"}
          </button>
        </p>
      ) : null}
      <p className="muted used-by-note">{NOTE}</p>
    </>
  );
}
