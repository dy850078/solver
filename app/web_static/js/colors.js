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

export function rebuildColorScale(agSet, clusterSet = new Set()) {
  [agOrder, agSlot] = assign(agSet);
  [clusterOrder, clusterSlot] = assign(clusterSet);
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

/** "cluster-3" → "c3"; anything else keeps a short readable stem. */
export function clusterShort(cl) {
  if (!cl) return "?";
  if (cl === "shared") return "sh";
  const m = /^cluster-(\d+)$/.exec(cl);
  return m ? `c${m[1]}` : cl.slice(0, 4);
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
