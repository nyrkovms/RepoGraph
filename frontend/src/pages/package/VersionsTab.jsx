import { useState } from "react";

import { ActionLink } from "./Links";
import { LINES_SHOWN } from "./packageEnv";
import { formatDate, packagingOf } from "./packageHelpers";
import { VersionStatus } from "./VersionStatus";

// The chips of the tab and the view each of them opens (SPEC-CATALOG-PAGES.md §5). Counts come from the overview; a
// chip with none is hidden, except All. Without an overview (it failed) only All is left.
export function versionChips(overview, isRpm) {
  const counts = overview?.versions;
  const chips = [];
  if (!isRpm && overview?.lines?.length) {
    chips.push({ value: "lines", label: "Lines" });
  }
  if (counts?.prereleases > 0) {
    chips.push({ value: "prereleases", label: "Prereleases", count: counts.prereleases });
  }
  if (counts?.not_active > 0) {
    chips.push({ value: "not-active", label: "Not active", count: counts.not_active });
  }
  if (counts?.deprecated > 0) {
    chips.push({ value: "deprecated", label: "Deprecated", count: counts.deprecated });
  }
  chips.push({ value: "all", label: "All", count: counts?.total });
  return chips;
}

// The view that is shown: the requested one if it has a chip, else Lines, or All where the package has no lines.
export function resolveVersionsView(requested, chips) {
  if (chips.some((chip) => chip.value === requested)) {
    return requested;
  }
  return chips.some((chip) => chip.value === "lines") ? "lines" : "all";
}

function Chips({ chips, view, tabHref, onSelectView }) {
  return (
    <nav className="chips" aria-label="Versions view">
      {chips.map((chip) => (
        <ActionLink
          key={chip.value}
          href={tabHref("versions", { view: chip.value === "lines" ? null : chip.value, line: null })}
          onSelect={() => onSelectView(chip.value)}
          className={`chip${chip.value === view ? " active" : ""}`}
          aria-current={chip.value === view ? "true" : undefined}
        >
          {chip.count === undefined ? chip.label : `${chip.label} ${chip.count}`}
        </ActionLink>
      ))}
    </nav>
  );
}

function VersionRow({ row, name, selectedVersion, tabHref, onSelectVersion, isGo, isMaven, isRpm, archesByVersion }) {
  return (
    <li className="version-row">
      <ActionLink href={tabHref(undefined, { version: row.version })} onSelect={() => onSelectVersion(row.version)}>
        {row.version}
      </ActionLink>
      {row.version === selectedVersion ? <span className="tag viewing">viewing</span> : null}
      {row.is_prerelease ? <span className="tag cond">prerelease</span> : null}
      <VersionStatus version={row} isGo={isGo} hideActive />
      {isMaven && packagingOf(row) ? <span className="tag cond">{packagingOf(row)}</span> : null}
      {isRpm
        ? (archesByVersion[`${name}/${row.version}`] || []).map((item) => (
            <span key={item} className="tag cond">
              {item}
            </span>
          ))
        : null}
      {formatDate(row.release_date) ? <span className="muted version-date">{formatDate(row.release_date)}</span> : null}
    </li>
  );
}

function ListState({ list, empty }) {
  return (
    <>
      {list.state === "loading" ? <p className="muted">Loading...</p> : null}
      {list.state === "error" ? <p className="error">Versions are not available</p> : null}
      {list.state === "ready" && list.rows.length === 0 ? <p>{empty}</p> : null}
    </>
  );
}

function LinesTable({ lines, expanded, viewingLine, lineList, tabHref, onSelectLine, rowProps }) {
  const [showAll, setShowAll] = useState(false);
  // An expanded line among the older ones keeps them open, so the address always shows what it points to.
  const hiddenExpanded = expanded && lines.findIndex((item) => item.line === expanded) >= LINES_SHOWN;
  const shown = showAll || hiddenExpanded ? lines : lines.slice(0, LINES_SHOWN);
  return (
    <>
      <div className="table-wrap">
        <table className="used-by-table lines-table">
          <thead>
            <tr>
              <th>Line</th>
              <th>Latest</th>
              <th>Released</th>
              <th>Stable releases</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((item) => {
              const open = item.line === expanded;
              return [
                <tr key={item.line} className={open ? "line-open" : undefined}>
                  <td>
                    <ActionLink
                      href={tabHref("versions", { line: open ? null : item.line })}
                      onSelect={() => onSelectLine(open ? "" : item.line)}
                      aria-expanded={open}
                    >
                      {item.line}
                    </ActionLink>
                    {item.line === viewingLine ? <span className="tag viewing">viewing</span> : null}
                  </td>
                  <td>{item.latest}</td>
                  <td>{formatDate(item.released_at)}</td>
                  <td>{item.stable_releases}</td>
                  <td>{formatDate(item.started_at)}</td>
                </tr>,
                open ? (
                  <tr key={`${item.line}-versions`} className="line-versions">
                    <td colSpan={5}>
                      <ListState list={lineList} empty="No versions in this line" />
                      {lineList.rows.length > 0 ? (
                        <ul className="version-rows">
                          {lineList.rows.map((row) => (
                            <VersionRow key={row.version} row={row} {...rowProps} />
                          ))}
                        </ul>
                      ) : null}
                    </td>
                  </tr>
                ) : null,
              ];
            })}
          </tbody>
        </table>
      </div>
      {!showAll && !hiddenExpanded && lines.length > LINES_SHOWN ? (
        <p>
          <button type="button" onClick={() => setShowAll(true)}>
            {`Show ${lines.length - LINES_SHOWN} older lines`}
          </button>
        </p>
      ) : null}
    </>
  );
}

// Release lines, or a flat list under a filter; `list` is the flat list of `view`, `lineList` the expanded line's.
export function VersionsTab({
  overview, chips, view, expandedLine, viewingLine, list, lineList, tabHref, onSelectView, onSelectLine, ...rowProps
}) {
  return (
    <>
      {chips.length > 1 ? <Chips chips={chips} view={view} tabHref={tabHref} onSelectView={onSelectView} /> : null}
      {view === "lines" ? (
        <LinesTable
          lines={overview.lines}
          expanded={expandedLine}
          viewingLine={viewingLine}
          lineList={lineList}
          tabHref={tabHref}
          onSelectLine={onSelectLine}
          rowProps={{ tabHref, ...rowProps }}
        />
      ) : (
        <>
          <ListState list={list} empty="No versions" />
          {list.rows.length > 0 ? (
            <ul className="version-rows">
              {list.rows.map((row) => (
                <VersionRow key={row.version} row={row} tabHref={tabHref} {...rowProps} />
              ))}
            </ul>
          ) : null}
          {list.hasMore ? (
            <p>
              <button type="button" onClick={list.loadMore} disabled={list.loadingMore}>
                {list.loadingMore ? "Loading..." : "Load more"}
              </button>
            </p>
          ) : null}
        </>
      )}
    </>
  );
}
