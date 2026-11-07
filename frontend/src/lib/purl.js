import { searchPackages } from "./api";
import { packageHref } from "./packageNames";

export function isPurl(q) {
  return String(q || "").trim().slice(0, 4).toLowerCase() === "pkg:";
}

// The version a purl names (`pkg:npm/express@4.18.2?arch=x` -> `4.18.2`), or "". The scope's `@` is encoded in a purl
// (`%40angular`), but one typed by hand may carry it raw, so only an `@` after the last `/` starts a version.
export function purlVersion(purl) {
  const path = String(purl || "").trim().replace(/[?#].*$/, "");
  const tail = path.slice(path.lastIndexOf("/") + 1);
  const at = tail.indexOf("@");
  if (at < 0) {
    return "";
  }
  try {
    return decodeURIComponent(tail.slice(at + 1));
  } catch {
    return tail.slice(at + 1);
  }
}

// Where a purl typed in a search box leads: the card of the one package it names (at its version, if it names one), or
// null when the catalog has none or more than one, so the search page can say so. A failed request throws.
export async function purlDestination(q) {
  const payload = await searchPackages({ q, limit: 2 });
  if (payload.data?.length !== 1) {
    return null;
  }
  const href = packageHref(payload.data[0]);
  const version = purlVersion(q);
  return version ? `${href}?version=${encodeURIComponent(version)}` : href;
}
