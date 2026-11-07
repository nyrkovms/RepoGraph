// Internal key of the version's feature map, not a feature.
const HIDDEN_FEATURE_KEYS = new Set(["dependencies_hydrated"]);

// Cargo feature map of a version: feature name -> what it enables (other features, `dep:crate`, `crate/feature`).
function featureEntries(features) {
  if (!features || typeof features !== "object") {
    return [];
  }
  return Object.entries(features)
    .filter(([name]) => !HIDDEN_FEATURE_KEYS.has(name))
    .sort(([left], [right]) => Number(right === "default") - Number(left === "default") || left.localeCompare(right));
}

export function FeaturesBlock({ version, features }) {
  const entries = featureEntries(features);
  return (
    <div className="version-features">
      <h3>Features of {version}</h3>
      {entries.length === 0 ? <p className="muted">No features.</p> : null}
      {entries.length ? (
        <dl className="feature-list">
          {entries.map(([name, enables]) => {
            const list = Array.isArray(enables) ? enables : [];
            return (
              <div key={name} className="feature-item">
                <dt>
                  <code>{name}</code>
                </dt>
                <dd>{list.length ? list.map((item) => <code key={item}>{item}</code>) : <span className="muted">enables nothing</span>}</dd>
              </div>
            );
          })}
        </dl>
      ) : null}
    </div>
  );
}
