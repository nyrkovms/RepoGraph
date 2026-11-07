import { overviewSummary } from "../package/overviewSummary";

const overview = (patch = {}) => ({
  dependencies: { direct: 4, optional: 2, indirect: 3, not_in_catalog: 1 },
  used_by: { direct: 1, indirect: 2 },
  activity: [{ month: "2026-10", releases: 7 }, { month: "2026-11", releases: 12 }],
  ...patch,
});

describe("overviewSummary", () => {
  it("writes one sentence from the direct numbers, never the indirect ones", () => {
    expect(overviewSummary(overview(), { ecosystem: "pypi" })).toBe(
      "Depends on 4 packages directly. 1 package in this catalog uses it. 19 releases in the last 12 months."
    );
  });

  it("uses plural and singular forms", () => {
    const text = overviewSummary(
      overview({ dependencies: { direct: 1 }, used_by: { direct: 5 }, activity: [{ month: "2026-11", releases: 1 }] }),
      {}
    );
    expect(text).toBe("Depends on 1 package directly. 5 packages in this catalog use it. 1 release in the last 12 months.");
  });

  it("says plainly when there is nothing", () => {
    const text = overviewSummary(overview({ dependencies: { direct: 0 }, used_by: { direct: 0 } }), {});
    expect(text).toBe("Has no dependencies. No packages in this catalog use it. 19 releases in the last 12 months.");
  });

  it("skips a clause whose data is null or missing", () => {
    expect(overviewSummary(overview({ dependencies: { direct: null }, activity: undefined }), {})).toBe(
      "1 package in this catalog uses it."
    );
    expect(overviewSummary(null, {})).toBe("");
  });

  it("says nothing of indirect numbers for RPM and calls its dependents direct", () => {
    const rpm = overview({ used_by: { direct: 3, indirect: null }, dependencies: { direct: 6, indirect: null } });
    const text = overviewSummary(rpm, { ecosystem: "rpm" });
    expect(text).toBe("Depends on 6 packages directly. 3 packages in this catalog use it directly. 19 releases in the last 12 months.");
    expect(text).not.toMatch(/indirect/i);
  });
});
