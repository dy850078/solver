/**
 * Categorical color assignment over the validated slot tokens
 * (--cat-1..8 + per-slot inks, themed in theme.css).
 *
 * Returned values are var() references, so light/dark resolution happens
 * in CSS and a theme toggle needs no re-render. Slots are assigned in
 * fixed sorted order and NEVER cycled — values beyond 8 share the neutral
 * slot, where the text label (chip badge / legend) still carries identity.
 *
 * Two dimensions share the one palette but never the same treatment:
 * clusters render as SOLID badges, AGs as ~15% TINTS — the treatment,
 * not a second palette, is what separates them visually.
 */

const SLOTS = 8;

let agOrder = [];
let agSlot = new Map();
let clusterOrder = [];
let clusterSlot = new Map();

// Natural (numeric-aware) order, so "cluster-10" sorts after "cluster-9".
// Plain lexicographic sort put cluster-10 second, letting it steal a color
// slot while cluster-8/9 both overflowed into the neutral gray.
const naturalCompare = new Intl.Collator(undefined, { numeric: true }).compare;

function assign(values) {
  const order = Array.from(values).filter(Boolean).sort(naturalCompare);
  return [order, new Map(order.map((v, i) => [v, i < SLOTS ? i + 1 : null]))];
}

let clusterShortMap = new Map();

export function rebuildColorScale(agSet, clusterSet = new Set()) {
  [agOrder, agSlot] = assign(agSet);
  [clusterOrder, clusterSlot] = assign(clusterSet);
  clusterShortMap = buildShortNames(clusterOrder);
}

/** Longest prefix shared by every name, cut back to a separator boundary
 *  (so "prod-taipei-dc-01" / "prod-taipei-dc-02" share "prod-taipei-dc-"). */
function commonPrefix(names) {
  if (names.length < 2) return "";
  let p = names[0];
  for (const n of names.slice(1)) {
    let i = 0;
    while (i < p.length && i < n.length && p[i] === n[i]) i += 1;
    p = p.slice(0, i);
    if (!p) return "";
  }
  const cut = Math.max(p.lastIndexOf("-"), p.lastIndexOf("_"), p.lastIndexOf("."), p.lastIndexOf("/"));
  return cut >= 0 ? p.slice(0, cut + 1) : "";
}

/* Badge text per cluster. The badge is for colour scanning, the text only
 * confirms identity, so it stays short — but it must stay UNIQUE:
 *   1. "cluster-N" → "cN", "shared" → "sh" (legacy shapes);
 *   2. otherwise drop the prefix every cluster shares (…-01 / …-02 differ
 *      only in the tail), take the stem whole when it is ≤ 6 chars, else
 *      its first 4 chars;
 *   3. if two clusters still collide, lengthen the colliding ones one char
 *      at a time until they differ (or the stem runs out).
 * The legend and the chip tooltip always carry the full name. */
function buildShortNames(order) {
  const names = order.filter(Boolean);
  const legacy = (cl) => {
    if (cl === "shared") return "sh";
    const m = /^cluster-(\d+)$/.exec(cl);
    return m ? `c${m[1]}` : null;
  };
  const shortOf = (s) => (s.length <= 6 ? s : s.slice(0, 4));
  const stripped = (cl, prefix) => (prefix && cl.startsWith(prefix) && cl.length > prefix.length ? cl.slice(prefix.length) : cl);
  // stem = the part of the name that is allowed to show; short = what shows.
  const globalPrefix = commonPrefix(names);
  const entry = new Map(names.map((cl) => {
    const stem = stripped(cl, globalPrefix);
    return [cl, { stem, short: legacy(cl) ?? shortOf(stem) }];
  }));
  for (let round = 0; round < 64; round += 1) {
    const bySh = new Map();
    for (const [cl, e] of entry) (bySh.get(e.short) ?? bySh.set(e.short, []).get(e.short)).push(cl);
    const groups = [...bySh.values()].filter((cls) => cls.length > 1);
    if (!groups.length) break;
    let changed = false;
    for (const cls of groups) {
      // 1st try: the colliding names usually share a longer prefix than the
      // whole set does (…-01 / …-02 next to an unrelated "A1"): strip it.
      const p = commonPrefix(cls);
      const narrowed = cls.filter((cl) => p && entry.get(cl).stem.length > stripped(cl, p).length);
      if (narrowed.length) {
        for (const cl of narrowed) {
          const stem = stripped(cl, p);
          entry.set(cl, { stem, short: legacy(cl) ?? shortOf(stem) });
        }
        changed = true;
        continue;
      }
      // 2nd try: grow the colliding shorts one char at a time from the stem.
      for (const cl of cls) {
        const e = entry.get(cl);
        if (e.short.length < e.stem.length) { e.short = e.stem.slice(0, e.short.length + 1); changed = true; }
      }
    }
    if (!changed) break;
  }
  return new Map([...entry].map(([cl, e]) => [cl, e.short]));
}

const colorOf = (map, key) => {
  const s = map.get(key);
  return s ? `var(--cat-${s})` : "var(--cat-none)";
};
const inkOf = (map, key) => {
  const s = map.get(key);
  return s ? `var(--cat-${s}-ink)` : "var(--cat-none-ink)";
};

export function colorForAg(ag) {
  return ag ? colorOf(agSlot, ag) : "var(--cat-none)";
}
export function colorForCluster(cl) {
  return cl ? colorOf(clusterSlot, cl) : "var(--cat-none)";
}
export function inkForCluster(cl) {
  return cl ? inkOf(clusterSlot, cl) : "var(--cat-none-ink)";
}

/** Badge text for a cluster (see buildShortNames). Clusters not seen by the
 *  last rebuildColorScale() fall back to the stand-alone rule. */
export function clusterShort(cl) {
  if (!cl) return "?";
  const known = clusterShortMap.get(cl);
  if (known) return known;
  if (cl === "shared") return "sh";
  const m = /^cluster-(\d+)$/.exec(cl);
  return m ? `c${m[1]}` : cl.length <= 6 ? cl : cl.slice(0, 4);
}

export function legendEntries() {
  return agOrder.map((ag) => ({ ag, color: colorForAg(ag) }));
}
export function clusterLegendEntries() {
  return clusterOrder.map((cl) => ({
    cluster: cl,
    short: clusterShort(cl),
    color: colorForCluster(cl),
    ink: inkForCluster(cl),
  }));
}
