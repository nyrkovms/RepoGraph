import { useCallback, useEffect, useRef, useState } from "react";

import { fetchVersions } from "../../lib/api";

export const VERSIONS_PAGE = 50;
const EMPTY = { rows: [], cursor: null, hasMore: false, state: "loading", loadingMore: false };

// What a list key asks the API for. `line:2.14` takes no page: the API returns the whole line.
function paramsOf(key) {
  if (key.startsWith("line:")) {
    return { line: key.slice(5) };
  }
  if (key === "prereleases") {
    return { prerelease: true, limit: VERSIONS_PAGE };
  }
  if (key === "not-active") {
    return { not_active: true, limit: VERSIONS_PAGE };
  }
  return { limit: VERSIONS_PAGE };
}

// A list of versions of the package for one `listKey` ("all", "prereleases", "not-active" or "line:<line>"), loaded when
// `listKey` is set and kept for the package: going back to a list does not ask again. Each key has its own cursor.
// `seed` ({ rows, cursor, hasMore }) is a first page of "all" the page already has, so that list does not ask for it.
export function useVersionList(ecosystem, name, listKey, seed = null) {
  const key = `${ecosystem}/${name}`;
  // The store belongs to one package: for another one it reads as empty, and a late answer for the old one is dropped.
  const [store, setStore] = useState({ key, entries: {} });
  const inflight = useRef(new Set());
  const current = store.key === key ? store : { key, entries: {} };
  const seeded = seed && listKey === "all" ? { ...seed, state: "ready", loadingMore: false } : null;

  const load = useCallback(
    (which, cursor) => {
      const change = (update) => setStore((before) => update(before.key === key ? before : { key, entries: {} }));
      fetchVersions(ecosystem, name, { ...paramsOf(which), cursor })
        .then((payload) =>
          change((base) => {
            const before = cursor ? base.entries[which] || (which === "all" && seeded) || EMPTY : EMPTY;
            const entry = {
              rows: [...before.rows, ...(payload.data || [])],
              cursor: payload.cursor,
              hasMore: Boolean(payload.has_more),
              state: "ready",
              loadingMore: false,
            };
            return { ...base, entries: { ...base.entries, [which]: entry } };
          })
        )
        .catch(() =>
          change((base) => ({
            ...base,
            entries: { ...base.entries, [which]: { ...(base.entries[which] || EMPTY), state: "error", loadingMore: false } },
          }))
        )
        .finally(() => inflight.current.delete(`${key}|${which}`));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [ecosystem, name, key, seed]
  );

  const known = listKey ? current.entries[listKey] || seeded || undefined : undefined;
  useEffect(() => {
    const id = `${key}|${listKey}`;
    if (listKey && !known && !inflight.current.has(id)) {
      inflight.current.add(id);
      load(listKey, undefined);
    }
  }, [listKey, key, known, load]);

  const entry = known || EMPTY;
  const loadMore = () => {
    if (listKey && entry.hasMore && entry.cursor && !entry.loadingMore) {
      setStore({ ...current, entries: { ...current.entries, [listKey]: { ...entry, loadingMore: true } } });
      load(listKey, entry.cursor);
    }
  };
  return { ...entry, loadMore };
}
