import { useEffect, useRef, useState } from "react";

import { fetchCompare } from "../../lib/api";
import { VersionStatus } from "./VersionStatus";
import { formatDate } from "./packageHelpers";

// The versions below `to` in the loaded list (newest first), plus the current `from` when it is outside them.
function fromOptions(versions, to, from) {
  const at = versions.findIndex((row) => row.version === to);
  const names = (at < 0 ? versions : versions.slice(at + 1)).map((row) => row.version);
  return from && !names.includes(from) ? [from, ...names] : names;
}

function Side({ info, isGo }) {
  return (
    <>
      {formatDate(info.release_date) || "unknown date"} <VersionStatus version={info} isGo={isGo} hideActive />
    </>
  );
}

function Message({ children }) {
  return <p className="muted">{children}</p>;
}

function DiffTable({ data }) {
  const { added, removed, changed } = data.dependencies;
  return (
    <div className="table-wrap">
      <table className="used-by-table compare-table">
        <thead>
          <tr>
            <th>Dependency</th>
            <th>{data.from.version}</th>
            <th>{data.to.version}</th>
          </tr>
        </thead>
        <tbody>
          {changed.map((row) => (
            <tr key={`changed:${row.dependency_type}:${row.name}`}>
              <td>
                {row.name} <span className="tag muted-tag">{row.dependency_type}</span>
              </td>
              <td className="diff-old">
                <code>{row.from}</code>
              </td>
              <td className="diff-new">
                <code>{row.to}</code>
              </td>
            </tr>
          ))}
          {removed.map((row) => (
            <tr key={`removed:${row.dependency_type}:${row.name}`}>
              <td>
                {row.name} <span className="tag muted-tag">{row.dependency_type}</span>
              </td>
              <td className="diff-old">
                <span className="diff-marker">removed</span>
                <code>{row.constraint}</code>
              </td>
              <td />
            </tr>
          ))}
          {added.map((row) => (
            <tr key={`added:${row.dependency_type}:${row.name}`}>
              <td>
                {row.name} <span className="tag muted-tag">{row.dependency_type}</span>
              </td>
              <td />
              <td className="diff-new">
                <span className="diff-marker">added</span>
                <code>{row.constraint}</code>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// What changed in the dependencies between `from` and `to` (the header's version). An empty `from` is the API's default,
// the nearest lower stable version; the answer says which one that is. `hold` keeps the request back while the page is
// still hydrating `to` for the tree, so two requests do not load one version's dependencies at once.
export function CompareTab({ ecosystem, name, to, from, hold = false, versions, onSelectFrom, isGo }) {
  // An answer is kept with the request it belongs to: for another one the tab reads as loading, and an answer that
  // arrives after a newer request was made is dropped.
  const requestKey = `${ecosystem}/${name}|${from}|${to}`;
  const [answer, setAnswer] = useState({ key: "", state: "loading", data: null });
  const latestKey = useRef("");

  useEffect(() => {
    if (!to || hold) {
      return;
    }
    latestKey.current = requestKey;
    const settle = (state, data) => {
      if (latestKey.current === requestKey) {
        setAnswer({ key: requestKey, state, data });
      }
    };
    fetchCompare(ecosystem, name, { from, to })
      .then((data) => settle("ready", data))
      .catch(() => settle("error", null));
  }, [ecosystem, name, from, to, hold, requestKey]);

  const result = answer.key === requestKey ? answer : { state: "loading", data: null };
  const data = result.state === "ready" ? result.data : null;
  const shownFrom = from || data?.from?.version || "";
  const options = fromOptions(versions, to, shownFrom);
  const { added, removed, changed, unchanged } = data?.dependencies || {};
  const differences = data ? added.length + removed.length + changed.length : 0;

  return (
    <>
      <div className="compare-controls">
        <span className="compare-from">
          <label htmlFor="compare-from">From</label>
          <select id="compare-from" value={shownFrom} onChange={(event) => onSelectFrom(event.target.value)}>
            {shownFrom ? null : <option value="">{data ? "no lower version" : "previous stable"}</option>}
            {options.map((version) => (
              <option key={version} value={version}>
                {version}
              </option>
            ))}
          </select>
        </span>
        <span className="compare-to">
          To <b>{to}</b> <span className="muted">change it above</span>
        </span>
      </div>
      {from === "" && data?.from ? <p className="muted">Compared with the previous stable version, {data.from.version}</p> : null}
      {data?.from ? (
        <p className="muted compare-dates">
          released <Side info={data.from} isGo={isGo} /> → <Side info={data.to} isGo={isGo} />
        </p>
      ) : null}

      {result.state === "loading" && !data ? <Message>Loading...</Message> : null}
      {result.state === "error" ? <p className="error">Could not compare these versions</p> : null}
      {data && !data.from ? <Message>This is the earliest stable version</Message> : null}
      {data?.from && data.dependencies_unknown ? <Message>The source has no dependency data for one of these versions</Message> : null}
      {data?.from && !data.dependencies_unknown ? (
        <>
          {differences ? <DiffTable data={data} /> : <Message>No dependency changes between these versions</Message>}
          <p className="muted">{`${unchanged} ${unchanged === 1 ? "dependency" : "dependencies"} unchanged`}</p>
        </>
      ) : null}
      <p className="muted">License per version is not stored</p>
    </>
  );
}
