import { installCommand } from "../../lib/installCommand";
import { CopyField } from "./CopyField";
import { formatDate, isWebUrl, versionedPurl } from "./packageHelpers";

function Fact({ label, children }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

// A web address as a link; anything else (git://, ssh) as plain text.
function Address({ url }) {
  return isWebUrl(url) ? (
    <a href={url} target="_blank" rel="noreferrer noopener">
      {url.replace(/^https?:\/\//i, "").replace(/\/$/, "")}
    </a>
  ) : (
    url
  );
}

// The side panel of the card: what the package is and how to take it. Counts live on the tabs and in the Overview.
// Empty facts are left out; `License` is the exception, it names the gap.
export function PackageFacts({ ecosystem, pkg, selectedVersion, versionRow, versionDetail }) {
  const released = formatDate(versionRow?.release_date);
  const license = pkg.license_spdx || pkg.license_raw || "Not published by the source";
  const purl = pkg.purl && selectedVersion ? versionDetail?.purl || versionedPurl(pkg.purl, selectedVersion) : "";
  const install = selectedVersion ? installCommand(ecosystem, pkg, selectedVersion) : "";
  return (
    <aside className="package-facts" aria-label="Package facts">
      <dl>
        {released ? <Fact label="Released">{released}</Fact> : null}
        <Fact label="License">{license}</Fact>
        {pkg.repository_url ? <Fact label="Repository"><Address url={pkg.repository_url} /></Fact> : null}
        {pkg.homepage_url ? <Fact label="Homepage"><Address url={pkg.homepage_url} /></Fact> : null}
        {pkg.documentation_url ? <Fact label="Documentation"><Address url={pkg.documentation_url} /></Fact> : null}
        {ecosystem === "maven" ? (
          <>
            <Fact label="groupId"><code>{pkg.namespace}</code></Fact>
            <Fact label="artifactId"><code>{pkg.normalized_name}</code></Fact>
          </>
        ) : null}
        {ecosystem === "rpm" ? (
          <>
            <Fact label="Distro"><code>{pkg.namespace}</code></Fact>
            <Fact label="Name"><code>{pkg.normalized_name}</code></Fact>
          </>
        ) : null}
        {install ? <Fact label="Install"><CopyField label="Install" text={install} /></Fact> : null}
        {purl ? <Fact label="purl"><CopyField label="purl" text={purl} /></Fact> : null}
      </dl>
    </aside>
  );
}
