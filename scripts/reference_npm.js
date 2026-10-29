// Reduces `npm install --package-lock-only` to the packages npm installs on this platform (run inside the case's
// docker platform): the lockfile is cross-platform and keeps optional packages for other os/cpu, so those and what is
// only reachable through them are dropped by walking the tree with node's module resolution from the root.
const fs = require("fs");
const lock = JSON.parse(fs.readFileSync("package-lock.json", "utf8"));
const pkgs = lock.packages;
const platformOk = (entry) => {
  const check = (list, value) => {
    if (!list || !list.length) return true;
    if (list.includes("!" + value)) return false;
    const positive = list.filter((v) => !v.startsWith("!"));
    return !positive.length || positive.includes(value);
  };
  return check(entry.os, process.platform) && check(entry.cpu, process.arch);
};
const resolve = (from, name) => {
  let base = from;
  while (true) {
    const candidate = (base ? base + "/" : "") + "node_modules/" + name;
    if (pkgs[candidate]) return candidate;
    if (!base) return null;
    const idx = base.lastIndexOf("/node_modules/");
    base = idx >= 0 ? base.slice(0, idx) : "";
  }
};
const seen = new Set();
const queue = [""];
while (queue.length) {
  const path = queue.shift();
  const entry = pkgs[path];
  const deps = { ...(entry.dependencies || {}), ...(entry.optionalDependencies || {}), ...(entry.peerDependencies || {}) };
  const optionalPeers = Object.entries(entry.peerDependenciesMeta || {}).filter(([, m]) => m.optional).map(([n]) => n);
  for (const name of Object.keys(deps)) {
    if (optionalPeers.includes(name)) continue;
    const target = resolve(path, name);
    if (!target || seen.has(target)) continue;
    if (!platformOk(pkgs[target])) continue;
    seen.add(target);
    queue.push(target);
  }
}
const install = [...seen].map((p) => {
  const e = pkgs[p];
  return { name: e.name || p.slice(p.lastIndexOf("node_modules/") + 13), version: e.version };
}).sort((a, b) => (a.name + a.version).localeCompare(b.name + b.version));
process.stdout.write(JSON.stringify(install));
