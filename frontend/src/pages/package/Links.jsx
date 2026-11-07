import { Link } from "react-router-dom";

import { relocationTarget } from "./packageHelpers";

export function RelocationLink({ alternative, isMaven }) {
  const target = isMaven ? relocationTarget(alternative) : null;
  if (!target) {
    return alternative;
  }
  const query = target.version ? `?version=${encodeURIComponent(target.version)}` : "";
  return <Link to={`/package/maven/${target.name}${query}`}>{alternative}</Link>;
}

// A link that changes page state (the tab, the version) and still carries the address it stands for, so it can be
// opened in a new tab or copied.
export function ActionLink({ href, onSelect, children, ...rest }) {
  function onClick(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    onSelect();
  }
  return (
    <a href={href} onClick={onClick} {...rest}>
      {children}
    </a>
  );
}
