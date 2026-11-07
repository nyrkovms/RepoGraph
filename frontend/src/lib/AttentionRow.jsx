import { Link } from "react-router-dom";

import { ecosystemLabel } from "./packageNames";
import { FLAG_LABELS, attentionReason, compareLink, displayName, ecosystemOf, flagsOf, packageLink } from "./updates";

// A package that needs a look, shared by the home page and Updates: what happened, why it matters and what to do.
// `lead` goes before the name (the Updates toggle); `children` are extra lines under the actions.
export default function AttentionRow({ item, lead = null, children = null }) {
  const flags = flagsOf(item);
  const usedBy = Number(item.used_by) || 0;
  return (
    <li className="attention-row">
      <div className="update-row-head">
        {lead}
        <Link to={packageLink(item)}>{displayName(item)}</Link>
        <span className="tag muted-tag">{ecosystemLabel(ecosystemOf(item))}</span>
        {item.to_version ? (
          <span className="update-versions">
            {item.from_version ? <code>{item.from_version}</code> : <span className="muted">new</span>}
            {" → "}
            <code>{item.to_version}</code>
          </span>
        ) : null}
        {flags.map((flag) => (
          <span key={flag} className={flag === "major" ? "tag warn-major" : "tag warn"}>
            {FLAG_LABELS[flag] || flag}
          </span>
        ))}
      </div>
      <p className="attention-reason">{attentionReason(flags)}</p>
      <p className="attention-actions">
        {item.from_version && item.to_version ? (
          <Link to={compareLink(item)}>
            Compare {item.from_version} → {item.to_version}
          </Link>
        ) : null}
        {usedBy > 0 ? (
          <Link to={`${packageLink(item)}?tab=used-by`}>Who uses it ({usedBy})</Link>
        ) : (
          <span className="muted">Nothing in this catalog depends on it</span>
        )}
      </p>
      {children}
    </li>
  );
}
