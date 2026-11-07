import { plural } from "./packageHelpers";

// The one sentence the Overview opens with, from the numbers the page already has. A clause whose data is null is
// skipped; indirect numbers are not part of it (the sections explain them). An RPM package says "directly" about its
// dependents: capabilities are not resolved, so there is no wider count to mean.
export function overviewSummary(overview, pkg) {
  if (!overview) return "";
  const clauses = [];
  const direct = overview.dependencies?.direct;
  if (direct !== null && direct !== undefined) {
    clauses.push(direct === 0 ? "Has no dependencies." : `Depends on ${plural(direct, "package")} directly.`);
  }
  const usedBy = overview.used_by?.direct;
  if (usedBy !== null && usedBy !== undefined) {
    clauses.push(
      usedBy === 0
        ? "No packages in this catalog use it."
        : `${plural(usedBy, "package")} in this catalog ${usedBy === 1 ? "uses" : "use"} it${pkg?.ecosystem === "rpm" ? " directly" : ""}.`
    );
  }
  if (Array.isArray(overview.activity)) {
    const releases = overview.activity.reduce((sum, item) => sum + item.releases, 0);
    clauses.push(`${plural(releases, "release")} in the last 12 months.`);
  }
  return clauses.join(" ");
}
