import { Link } from "react-router-dom";

import { isWebUrl, relocationTarget } from "./packageHelpers";

export function UrlValue({ url }) {
  if (!url) {
    return "n/a";
  }
  return isWebUrl(url) ? (
    <a href={url} target="_blank" rel="noreferrer noopener">
      {url}
    </a>
  ) : (
    url
  );
}

export function RelocationLink({ alternative, isMaven }) {
  const target = isMaven ? relocationTarget(alternative) : null;
  if (!target) {
    return alternative;
  }
  const query = target.version ? `?version=${encodeURIComponent(target.version)}` : "";
  return <Link to={`/package/maven/${target.name}${query}`}>{alternative}</Link>;
}
