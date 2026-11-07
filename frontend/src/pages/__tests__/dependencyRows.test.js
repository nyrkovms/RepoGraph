import { flattenTree, summarize } from "../package/dependencyRows";

function node(name, patch = {}) {
  return {
    name,
    constraint: "*",
    dependency_type: "runtime",
    resolved_version: "1.0.0",
    resolved_name: null,
    license: "MIT",
    cycle: false,
    children: [],
    ...patch,
  };
}

describe("flattenTree", () => {
  it("is empty for no nodes", () => {
    expect(flattenTree([])).toEqual([]);
    expect(flattenTree(undefined)).toEqual([]);
  });

  it("lists a package reached both directly and indirectly once, as Direct", () => {
    const rows = flattenTree([node("a", { children: [node("b")] }), node("b", { constraint: ">=1", dependency_type: "peer" })]);
    expect(rows.map((row) => [row.name, row.relation])).toEqual([
      ["a", "direct"],
      ["b", "direct"],
    ]);
    const b = rows.find((row) => row.name === "b");
    expect(b.path).toEqual([]);
    expect(b.requires).toEqual([">=1"]);
    expect(b.types).toEqual(["peer"]);
  });

  it("is Direct when the indirect occurrence comes first, and merges the direct edges of the package", () => {
    const rows = flattenTree([
      node("a", { children: [node("c", { constraint: "^1" })] }),
      node("c", { constraint: "^2" }),
      node("c", { constraint: "^2", dependency_type: "optional" }),
    ]);
    const c = rows.find((row) => row.name === "c");
    expect(c.relation).toBe("direct");
    expect(c.requires).toEqual(["^2"]);
    expect(c.types).toEqual(["runtime", "optional"]);
  });

  it("makes one row for a repeated package and keeps the path of its first occurrence", () => {
    const rows = flattenTree([
      node("a", { children: [node("x", { children: [node("z")] })] }),
      node("b", { children: [node("z")] }),
    ]);
    const z = rows.filter((row) => row.name === "z");
    expect(z).toHaveLength(1);
    expect(z[0].path).toEqual(["a", "x"]);
    expect(z[0].relation).toBe("indirect");
  });

  it("keeps one row per version of a package", () => {
    const rows = flattenTree([node("a", { children: [node("z", { resolved_version: "1.0.0" })] }), node("b", { children: [node("z", { resolved_version: "2.0.0" })] })]);
    expect(rows.filter((row) => row.name === "z").map((row) => row.resolved)).toEqual(["1.0.0", "2.0.0"]);
  });

  it("adds no row for a cycle node and still walks on", () => {
    const rows = flattenTree([
      node("a", { children: [node("b", { children: [node("a", { cycle: true }), node("c")] })] }),
    ]);
    expect(rows.map((row) => row.name)).toEqual(["a", "b", "c"]);
    expect(rows.find((row) => row.name === "c").path).toEqual(["a", "b"]);
  });

  it("gives a node without a resolved version its own row, not linked", () => {
    const rows = flattenTree([node("ghost", { resolved_version: null, license: null, children: [] }), node("a", { children: [node("ghost", { resolved_version: null })] })]);
    const ghosts = rows.filter((row) => row.name === "ghost");
    expect(ghosts).toHaveLength(1);
    expect(ghosts[0]).toMatchObject({ resolved: null, ref: null, relation: "direct" });
  });

  it("names a row by the catalog package an RPM capability resolves to", () => {
    const rows = flattenTree([
      node("libssl.so.3()(64bit)", { resolved_name: "rocky-9/openssl-libs" }),
      node("openssl-libs(x86-64)", { resolved_name: "rocky-9/openssl-libs" }),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ name: "rocky-9/openssl-libs", ref: "rocky-9/openssl-libs", capability: "libssl.so.3()(64bit)" });
  });

  it("puts direct rows first, then orders by name; indirect paths exclude the root", () => {
    const rows = flattenTree([
      node("m", { children: [node("a")] }),
      node("k"),
      node("z", { children: [node("b", { children: [node("c")] })] }),
    ]);
    expect(rows.map((row) => row.name)).toEqual(["k", "m", "z", "a", "b", "c"]);
    expect(rows.map((row) => row.path.join(" → "))).toEqual(["", "", "", "m", "z", "z → b"]);
  });
});

describe("summarize", () => {
  it("counts rows, those not in catalog and licenses of the resolved ones", () => {
    const rows = flattenTree([
      node("a", { license: "PSF-2.0", children: [node("b", { license: null }), node("c")] }),
      node("d"),
      node("ghost", { resolved_version: null, license: null }),
    ]);
    expect(summarize(rows)).toEqual({
      direct: 3,
      indirect: 2,
      notInCatalog: 1,
      licenses: [
        ["MIT", 2],
        ["PSF-2.0", 1],
        ["unknown", 1],
      ],
    });
  });
});
