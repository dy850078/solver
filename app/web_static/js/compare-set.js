// Compare set: the pure data side (no DOM). A set is the JSON document the
// Compare page edits, `POST /api/compare/run` consumes and
// `python -m app.compare` reads: catalogs (vm_specs / bm_models / bundles),
// shared `defaults`, and an explicit `scenarios` list referencing the
// catalogs by name (see docs/compare-sets.md, ADR-018).
//
// Also home of the browser draft (localStorage) so the Topology page can
// append a scenario without importing the whole Compare form.

export const DRAFT_KEY = "solver-compare-draft";

// GenerateRequest fields that come from the references, never from knobs.
export const RESERVED_KNOBS = ["vm_specs", "bm_profiles", "node_groups", "clusters"];

export function blankSet() {
  return {
    name: "",
    vm_specs: { standard: { cpu_cores: 8, memory_mib: 32000, storage_gb: 200, gpu: {} } },
    bm_models: { "A-64c": { capacity: { cpu_cores: 64, memory_mib: 256000, storage_gb: 2000, gpu: {} }, roles: [] } },
    bundles: {
      "cp-basic": [
        { role: "master", count: 3, ip_type: "routable", spec: "standard", max_per_bm: 1 },
        { role: "worker", count: 3, ip_type: "routable", spec: "standard" },
      ],
    },
    // tightness 1.0: procurement wants the fewest machines, so the generator
    // provisions no headroom and the solver's BM count is the figure to buy.
    defaults: { racks: 4, ags: 3, anti_affinity: true, target_spread: { ag: 3 }, tightness: 1.0 },
    scenarios: [
      { name: "A-64c · cp-basic · c1", bm_model: "A-64c", bundle: "cp-basic", clusters: 1 },
    ],
  };
}

export function loadDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const set = JSON.parse(raw);
    return set && typeof set === "object" && set.scenarios ? set : null;
  } catch {
    return null;
  }
}

export function saveDraft(set) {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(set)); } catch { /* private mode etc. */ }
}

export function clearDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
}

// "master5-infra5-l4lb-storage3": a bundle name derived from its groups.
export function bundleLabel(groups) {
  const parts = [];
  for (const g of groups || []) {
    if (!g || !(g.count > 0)) continue;
    parts.push(`${g.role}${g.count}`);
  }
  return parts.join("-") || "bundle";
}

export function modelsOf(sc) {
  return Array.isArray(sc.bm_model) ? sc.bm_model : [sc.bm_model];
}

export function scenarioAutoName(sc) {
  return `${modelsOf(sc).join("+")} · ${sc.bundle} · c${sc.clusters}`;
}

const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

// Insert `value` under `name` in a catalog; on a name clash with different
// content, suffix -2, -3, … so nothing is silently overwritten.
function catalogInsert(catalog, name, value) {
  const base = String(name || "item").replace(/[^\w.\-]+/g, "-").replace(/^-+|-+$/g, "") || "item";
  let key = base;
  let k = 2;
  while (key in catalog && !same(catalog[key], value)) key = `${base}-${k++}`;
  catalog[key] = value;
  return key;
}

function uniqueScenarioName(set, base) {
  const names = new Set((set.scenarios || []).map((s) => s.name));
  let name = base;
  let k = 2;
  while (names.has(name)) name = `${base} (${k++})`;
  return name;
}

/**
 * Add one mock-form configuration (a GenerateRequest params object, as
 * mockform.js's readMockParams returns it) to a set as a new scenario.
 * vm_specs merge into the catalog, each bm_profile becomes a BM model, the
 * node groups become a bundle, and every other knob goes to `defaults` when
 * the set has none yet, else to the scenario's `overrides` where it differs.
 * Returns the new scenario's name.
 */
export function addMockParams(set, params) {
  const p = { ...params };
  for (const [name, cap] of Object.entries(p.vm_specs || {})) {
    catalogInsert(set.vm_specs, name, cap);
  }
  const modelNames = (p.bm_profiles || []).map((prof) => {
    const { name, count, ...rest } = prof;   // count is dropped: models are always elastic
    void count;
    return catalogInsert(set.bm_models, name, { capacity: rest.capacity, roles: rest.roles || [] });
  });
  const groups = (p.node_groups || []).map((g) => ({ ...g }));
  const bundleName = catalogInsert(set.bundles, bundleLabel(groups), groups);
  const clusters = Number(p.clusters) || 1;

  const knobs = {};
  for (const [k, v] of Object.entries(p)) {
    if (RESERVED_KNOBS.includes(k) || v === undefined || v === null) continue;
    knobs[k] = v;
  }
  let overrides = {};
  if (!set.defaults || !Object.keys(set.defaults).length) {
    set.defaults = knobs;
  } else {
    for (const [k, v] of Object.entries(knobs)) {
      if (!same(set.defaults[k], v)) overrides[k] = v;
    }
  }
  const sc = {
    name: "",
    bm_model: modelNames.length === 1 ? modelNames[0] : modelNames,
    bundle: bundleName,
    clusters,
  };
  if (Object.keys(overrides).length) sc.overrides = overrides;
  sc.name = uniqueScenarioName(set, scenarioAutoName(sc));
  set.scenarios = set.scenarios || [];
  set.scenarios.push(sc);
  return sc.name;
}

// Strip a set to the wire format (drop UI-only keys, blank names).
export function normalizeSet(set) {
  const out = {
    name: set.name || "",
    vm_specs: set.vm_specs || {},
    bm_models: set.bm_models || {},
    bundles: set.bundles || {},
    defaults: set.defaults || {},
    scenarios: (set.scenarios || []).map((s) => {
      const sc = { name: s.name, bm_model: s.bm_model, bundle: s.bundle, clusters: Number(s.clusters) || 1 };
      if (s.overrides && Object.keys(s.overrides).length) sc.overrides = s.overrides;
      if (s.enabled === false) sc.enabled = false;
      return sc;
    }),
  };
  return out;
}
