import { useCallback, useEffect, useRef, useState } from "react";

import { fetchDependents } from "../../lib/api";

export const DEPENDENTS_PAGE = 50;
const EMPTY = { rows: [], cursor: null, hasMore: false, state: "loading", loadingMore: false };

// The dependents of the package, loaded when the tab is first opened and kept for the package: a tab switch or
// another version does not ask again. One entry per `relation` filter ("" is both), each with its own cursor.
export function useDependents(ecosystem, name, active, relation) {
  const key = `${ecosystem}/${name}`;
  // The store belongs to one package: for another one it reads as empty, and a late answer for the old one is dropped.
  const [store, setStore] = useState({ key, entries: {}, totals: null });
  const inflight = useRef(new Set());
  const current = store.key === key ? store : { key, entries: {}, totals: null };

  const load = useCallback(
    (filter, cursor) => {
      const change = (update) =>
        setStore((before) => {
          const base = before.key === key ? before : { key, entries: {}, totals: null };
          return update(base);
        });
      fetchDependents(ecosystem, name, { relation: filter, limit: DEPENDENTS_PAGE, cursor })
        .then((payload) =>
          change((base) => {
            const before = cursor ? base.entries[filter] || EMPTY : EMPTY;
            const entry = {
              rows: [...before.rows, ...(payload.data || [])],
              cursor: payload.cursor,
              hasMore: Boolean(payload.has_more),
              state: "ready",
              loadingMore: false,
            };
            return { ...base, totals: payload.totals, entries: { ...base.entries, [filter]: entry } };
          })
        )
        .catch(() =>
          change((base) => ({
            ...base,
            entries: { ...base.entries, [filter]: { ...(base.entries[filter] || EMPTY), state: "error", loadingMore: false } },
          }))
        )
        .finally(() => inflight.current.delete(`${key}|${filter}`));
    },
    [ecosystem, name, key]
  );

  const known = current.entries[relation];
  useEffect(() => {
    const id = `${key}|${relation}`;
    if (active && !known && !inflight.current.has(id)) {
      inflight.current.add(id);
      load(relation, undefined);
    }
  }, [active, relation, key, known, load]);

  const entry = known || EMPTY;
  const loadMore = () => {
    if (entry.hasMore && entry.cursor && !entry.loadingMore) {
      setStore({ ...current, entries: { ...current.entries, [relation]: { ...entry, loadingMore: true } } });
      load(relation, entry.cursor);
    }
  };
  return { ...entry, totals: current.totals, loadMore };
}
