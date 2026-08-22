/* Rollout builder form.
 *
 * Turns three small tables into a RolloutRequest:
 *   1. a named VM-spec catalog (define a size once, reuse it everywhere),
 *   2. an ordered list of build steps (one per cluster), each holding
 *      VM groups = role × ip_type × spec × count,
 *   3. a fleet template: one machine model plus topology counts
 *      (sites/phases/DCs/rooms/racks/AGs), generated the same way the mock
 *      generator does. Leaving the machine count blank switches to sizing
 *      mode — the caller asks the solver for the minimum fleet instead.
 *
 * The catalog is the answer to "the same spec keeps coming up": a group
 * row picks a spec by name, "⧉" clones a row, and cloning a whole step
 * copies its entire spec mix under a new cluster name.
 *
 * Steps produce EXPLICIT VMs (spec × count) because that is what
 * production scheduling calls carry. Coarse `requirements` (let the
 * splitter choose sizes for far-future demand) stay available through the
 * JSON escape hatch on the page.
 */

import { escapeHtml as esc } from "./util.js";

const GiB = 1024;
const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];
const IP_TYPES = ["routable", "non-routable"];

export const state = {
  specs: [],   // {name, cpu, memGiB, disk, gpu}
  steps: [],   // {name, groups:[{role, ipType, specIdx, count}]}
  // One homogeneous fleet described by topology counts, mirroring the mock
  // generator's knobs. `count` empty/null = "size it for me" (the sizing
  // endpoint decides); a number = fixed stock.
  fleet: {
    count: 6, cpu: 64, memGiB: 256, disk: 2000, gpu: 0,
    sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 3, ags: 3,
  },
  cfg: { autoAA: true, maxSolve: 10 },
};

const blank = {
  spec: () => ({ name: "", cpu: 8, memGiB: 32, disk: 200, gpu: 0 }),
  group: () => ({ role: "worker", ipType: "routable", specIdx: 0, count: 3 }),
};

/* Topology knobs, in hierarchy order. The four that default to 1 collapse
 * to a single bucket when left alone — harmless unless you spread on that
 * dimension (same convention as the mock generator). */
const TOPO_DIMS = [
  ["sites", "Sites"], ["phases", "Phases"], ["datacenters", "DCs"],
  ["rooms", "Rooms"], ["racks", "Racks"], ["ags", "AGs"],
];

export const specLabel = (s) =>
  `${s.name || "spec"} · ${s.cpu}c/${s.memGiB}g/${s.disk}gb${s.gpu ? `/${s.gpu}gpu` : ""}`;

/* ── field helpers ─────────────────────────────────────────────────── */

const bind = (sec, i, f, extra = "") =>
  `data-s="${sec}" data-i="${i}" data-f="${f}" ${extra}`;

const txt = (sec, i, f, v, label, ph = "") =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="text" value="${esc(v ?? "")}"
      placeholder="${esc(ph)}" ${bind(sec, i, f)}></label>`;

const num = (sec, i, f, v, label, min = 0) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="number" min="${min}" value="${v ?? 0}"
      ${bind(sec, i, f)}></label>`;

const pick = (sec, i, f, v, label, options) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <select class="select" ${bind(sec, i, f)}>${options.map((o) =>
      `<option value="${esc(o.value)}"${String(o.value) === String(v) ? " selected" : ""}>${esc(o.label)}</option>`,
    ).join("")}</select></label>`;

const act = (action, i, glyph, title, cls = "", extra = "") =>
  `<button type="button" class="row-act ${cls}" data-act="${action}" data-i="${i}"
     title="${esc(title)}" ${extra}>${glyph}</button>`;

/* ── row templates ─────────────────────────────────────────────────── */

function specRow(s, i) {
  return `<div class="frow-card"><div class="frow frow--spec">
    ${txt("specs", i, "name", s.name, "Name", "e.g. M-8c32g")}
    ${num("specs", i, "cpu", s.cpu, "vCore")}
    ${num("specs", i, "memGiB", s.memGiB, "Mem GiB")}
    ${num("specs", i, "disk", s.disk, "Disk GB")}
    ${act("spec-del", i, "✕", "Delete spec", "row-act--del")}
  </div></div>`;
}

const specOptionLabel = (s) => `${s.name || "spec"} · ${s.cpu}c/${s.memGiB}g`;

function groupRow(g, gi, si) {
  const specOpts = state.specs.map((s, idx) => ({ value: idx, label: specOptionLabel(s) }));
  const extra = `data-step="${si}"`;
  // Two lines: the sidebar is too narrow to fit role + ip + spec + count
  // on one row without truncating every label into "ma..".
  return `<div class="group-row">
    <div class="frow frow--group-top">
      ${pick(`groups.${si}`, gi, "role", g.role, "Role", ROLES.map((r) => ({ value: r, label: r })))}
      ${pick(`groups.${si}`, gi, "ipType", g.ipType, "IP type", IP_TYPES.map((t) => ({ value: t, label: t })))}
      ${num(`groups.${si}`, gi, "count", g.count, "Count", 1)}
      ${act("group-dup", gi, "⧉", "Duplicate this group", "", extra)}
      ${act("group-del", gi, "✕", "Delete group", "row-act--del", extra)}
    </div>
    ${pick(`groups.${si}`, gi, "specIdx", g.specIdx, "VM spec",
      specOpts.length ? specOpts : [{ value: 0, label: "— add a spec first —" }])}
  </div>`;
}

function stepCard(st, i) {
  const last = state.steps.length - 1;
  return `<div class="frow-card step-card">
    <div class="frow-card__head">
      <span class="step-card__order">
        <span class="step-card__seq">${i + 1}</span>
      </span>
      <div style="flex:1;min-width:0">
        <label class="mini"><span class="mini__label">Cluster / step name</span>
          <input class="input" type="text" value="${esc(st.name)}"
            placeholder="e.g. cluster-a" ${bind("steps", i, "name")}></label>
      </div>
      <span>
        ${act("step-up", i, "↑", "Move earlier", "", i === 0 ? "disabled" : "")}
        ${act("step-down", i, "↓", "Move later", "", i === last ? "disabled" : "")}
        ${act("step-dup", i, "⧉", "Duplicate step with its whole spec mix")}
        ${act("step-del", i, "✕", "Delete step", "row-act--del")}
      </span>
    </div>
    ${st.groups.map((g, gi) => groupRow(g, gi, i)).join("")}
    <button type="button" class="btn btn--add" data-act="group-add" data-i="${i}">
      + VM group
    </button>
  </div>`;
}

const fleetNum = (f, v, label, ph = "") =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="number" min="1" value="${v ?? ""}"
      placeholder="${esc(ph)}" data-fleet="${f}"></label>`;

function fleetCard() {
  const f = state.fleet;
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${fleetNum("count", f.count, "How many", "auto")}
      ${fleetNum("cpu", f.cpu, "vCore")}
      ${fleetNum("memGiB", f.memGiB, "Mem GiB")}
      ${fleetNum("disk", f.disk, "Disk GB")}
      <span></span>
    </div>
    <div class="frow frow--topo">
      ${TOPO_DIMS.map(([k, label]) => fleetNum(k, f[k], label, "1")).join("")}
    </div>
    <p class="form-empty">
      Leave <b>How many</b> blank to estimate the minimum fleet this rollout
      needs. Machines are spread round-robin over the racks, and racks over
      the AGs.
    </p>
  </div>`;
}

/* ── render ────────────────────────────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);

export function renderForm() {
  $("#spec-rows").innerHTML = state.specs.length
    ? state.specs.map(specRow).join("")
    : `<p class="form-empty">No specs yet — add one to size your VM groups.</p>`;
  $("#step-rows").innerHTML = state.steps.length
    ? state.steps.map(stepCard).join("")
    : `<p class="form-empty">No steps yet — add one cluster per build batch.</p>`;
  $("#fleet-card").innerHTML = fleetCard();
  $("#cfg-auto-aa").checked = state.cfg.autoAA;
  $("#cfg-max-solve").value = state.cfg.maxSolve;
  renderSummary();
}

function renderSummary() {
  const vms = state.steps.reduce(
    (n, st) => n + st.groups.reduce((m, g) => m + Math.max(0, g.count), 0), 0);
  const fleet = isSizingMode()
    ? "fleet size: estimate"
    : `${state.fleet.count} baremetal(s)`;
  $("#builder-summary").textContent =
    `${state.steps.length} step(s) · ${vms} VM(s) · ${fleet}`;
}

/** Blank/zero machine count means "work out the minimum for me". */
export function isSizingMode() {
  const c = state.fleet.count;
  return c === null || c === undefined || c === "" || Number(c) < 1;
}

/* ── mutation ──────────────────────────────────────────────────────── */

function coerce(el, raw) {
  if (el.type === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : 0;
  }
  if (el.type === "checkbox") return el.checked;
  return raw;
}

function applyFieldEdit(el) {
  const sec = el.dataset.s;
  const i = Number(el.dataset.i);
  const f = el.dataset.f;
  const value = coerce(el, el.value);
  if (sec.startsWith("groups.")) {
    const si = Number(sec.slice("groups.".length));
    const g = state.steps[si]?.groups?.[i];
    if (!g) return;
    g[f] = f === "specIdx" || f === "count" ? Number(value) : value;
  } else {
    const row = state[sec]?.[i];
    if (!row) return;
    row[f] = value;
  }
  // Spec names feed every group dropdown; step names feed nothing visual.
  if (sec === "specs" && f === "name") renderForm();
  else renderSummary();
}

function handleAction(action, i, stepIdx) {
  switch (action) {
    case "spec-add": state.specs.push(blank.spec()); break;
    case "spec-del":
      state.specs.splice(i, 1);
      // Keep group references valid: shift down, clamp at 0.
      for (const st of state.steps) {
        for (const g of st.groups) {
          if (g.specIdx === i) g.specIdx = 0;
          else if (g.specIdx > i) g.specIdx -= 1;
        }
      }
      break;
    case "step-add":
      state.steps.push({ name: `cluster-${state.steps.length + 1}`, groups: [blank.group()] });
      break;
    case "step-del": state.steps.splice(i, 1); break;
    case "step-dup": {
      const src = state.steps[i];
      state.steps.splice(i + 1, 0, {
        name: `${src.name}-copy`,
        groups: src.groups.map((g) => ({ ...g })),
      });
      break;
    }
    case "step-up":
      if (i > 0) state.steps.splice(i - 1, 0, state.steps.splice(i, 1)[0]);
      break;
    case "step-down":
      if (i < state.steps.length - 1) state.steps.splice(i + 1, 0, state.steps.splice(i, 1)[0]);
      break;
    case "group-add": state.steps[i]?.groups.push(blank.group()); break;
    case "group-del": {
      const st = state.steps[stepIdx];
      if (st && st.groups.length > 0) st.groups.splice(i, 1);
      break;
    }
    case "group-dup": {
      const st = state.steps[stepIdx];
      if (st) st.groups.splice(i + 1, 0, { ...st.groups[i] });
      break;
    }
    default: return false;
  }
  renderForm();
  return true;
}

/* ── request assembly ──────────────────────────────────────────────── */

const resources = (cpu, memGiB, disk, gpu = 0) => ({
  cpu_cores: Math.max(0, Math.round(cpu)),
  memory_mib: Math.max(0, Math.round(memGiB * GiB)),
  storage_gb: Math.max(0, Math.round(disk)),
  gpu_count: Math.max(0, Math.round(gpu)),
});

const dim = (v) => Math.max(1, Math.round(Number(v) || 1));

export function fleetCount() {
  return Math.max(0, Math.round(Number(state.fleet.count) || 0));
}

/**
 * Topology skeleton, one entry per rack. Every dimension above rack is
 * derived by modulo over the rack ordinal, so racks fan out across sites,
 * rooms and AGs simultaneously — the same rule the mock generator uses
 * (app/mockgen.py `_build_racks`). Each rack belongs to exactly one AG.
 */
export function buildRackTopologies() {
  const f = state.fleet;
  const racks = dim(f.racks);
  const out = [];
  for (let r = 0; r < racks; r++) {
    out.push({
      site: `site-${(r % dim(f.sites)) + 1}`,
      phase: `p${(r % dim(f.phases)) + 1}`,
      datacenter: `dc-${(r % dim(f.datacenters)) + 1}`,
      room: `room-${(r % dim(f.rooms)) + 1}`,
      rack: `rack-${r + 1}`,
      ag: `ag-${(r % dim(f.ags)) + 1}`,
    });
  }
  return out;
}

/** `n` identical machines round-robined over the rack skeleton. */
export function buildFleet(n) {
  const f = state.fleet;
  const racks = buildRackTopologies();
  const out = [];
  for (let i = 0; i < n; i++) {
    const seq = String(i + 1).padStart(3, "0");
    out.push({
      id: `bm-${seq}`,
      hostname: `bare-${seq}`,
      total_capacity: resources(f.cpu, f.memGiB, f.disk, f.gpu),
      used_capacity: resources(0, 0, 0),
      topology: { ...racks[i % racks.length] },
    });
  }
  return out;
}

/**
 * Assemble a RolloutRequest. Throws Error with a human message when the
 * form cannot produce a runnable request.
 *
 * In sizing mode there is no fleet yet, so callers use buildSizingRequest
 * instead; this function requires a concrete machine count.
 */
export function buildRequest() {
  if (isSizingMode()) {
    throw new Error("Machine count is blank — use sizing mode.");
  }
  return buildRequestWith(fleetCount(), { withCandidates: true });
}

function buildRequestWith(n, { withCandidates }) {
  if (!state.specs.length) throw new Error("Add at least one VM spec.");
  if (!state.steps.length) throw new Error("Add at least one build step.");
  const baremetals = buildFleet(n);
  if (withCandidates && !baremetals.length) {
    throw new Error("The fleet has zero baremetals.");
  }
  const allBmIds = baremetals.map((b) => b.id);

  const names = new Set();
  const steps = state.steps.map((st, si) => {
    const name = (st.name || "").trim();
    if (!name) throw new Error(`Step ${si + 1} needs a name.`);
    if (names.has(name)) throw new Error(`Duplicate step name "${name}".`);
    names.add(name);
    if (!st.groups.length) throw new Error(`Step "${name}" has no VM groups.`);

    const vms = [];
    st.groups.forEach((g, gi) => {
      const spec = state.specs[g.specIdx];
      if (!spec) throw new Error(`Step "${name}" group ${gi + 1} has no valid spec.`);
      const count = Math.max(0, Math.round(g.count));
      for (let k = 0; k < count; k++) {
        vms.push({
          // Step name prefix keeps ids unique across the whole rollout.
          id: `${name}-${g.role}-${gi + 1}-${k + 1}`,
          demand: resources(spec.cpu, spec.memGiB, spec.disk, spec.gpu),
          node_role: g.role,
          ip_type: g.ipType,
          cluster_id: name,
          ...(withCandidates ? { candidate_baremetals: allBmIds } : {}),
        });
      }
    });
    if (!vms.length) throw new Error(`Step "${name}" produces zero VMs (counts are 0).`);
    return { name, vms };
  });

  return {
    baremetals,
    steps,
    config: {
      auto_generate_anti_affinity: !!state.cfg.autoAA,
      max_solve_time_seconds: Math.max(1, Number(state.cfg.maxSolve) || 10),
    },
  };
}

/**
 * Assemble a RolloutSizingRequest: the same build plan, but the fleet is
 * described by the topology knobs and the machine count is the answer.
 * Candidate lists are deliberately omitted — the sizer generates a fresh
 * fleet per probe and fills them in.
 */
export function buildSizingRequest() {
  const req = buildRequestWith(0, { withCandidates: false });
  const f = state.fleet;
  return {
    fleet: {
      total_capacity: resources(f.cpu, f.memGiB, f.disk, f.gpu),
      sites: dim(f.sites), phases: dim(f.phases),
      datacenters: dim(f.datacenters), rooms: dim(f.rooms),
      racks: dim(f.racks), ags: dim(f.ags),
    },
    steps: req.steps,
    config: req.config,
  };
}

/* ── load an existing RolloutRequest back into the form ────────────── */

const specKey = (r) => `${r.cpu_cores}|${r.memory_mib}|${r.storage_gb}|${r.gpu_count ?? 0}`;

/**
 * Best-effort reverse mapping (used when an example/upload is loaded).
 * Returns false when the request uses features the form cannot express
 * (coarse requirements, brownfield existing_vms, explicit rules) — the
 * caller then keeps the JSON editor as the source of truth.
 */
export function loadIntoForm(req) {
  if (!req || !Array.isArray(req.steps) || !Array.isArray(req.baremetals)) return false;
  const expressible = req.steps.every(
    (s) => (s.vms?.length ?? 0) > 0 &&
           !(s.requirements?.length) &&
           !(s.anti_affinity_rules?.length) && !(s.max_per_bm_rules?.length) &&
           !(s.exclusive_bm_rules?.length) && !(s.failover_rules?.length),
  ) && !(req.existing_vms?.length);
  if (!expressible) return false;

  const specs = [];
  const byKey = new Map();
  const specIdxOf = (demand) => {
    const key = specKey(demand);
    if (!byKey.has(key)) {
      byKey.set(key, specs.length);
      specs.push({
        name: `spec-${specs.length + 1}`,
        cpu: demand.cpu_cores ?? 0,
        memGiB: Math.round((demand.memory_mib ?? 0) / GiB),
        disk: demand.storage_gb ?? 0,
        gpu: demand.gpu_count ?? 0,
      });
    }
    return byKey.get(key);
  };

  const steps = req.steps.map((s) => {
    // Collapse VMs into groups keyed by (role, ip_type, spec).
    const groups = new Map();
    for (const vm of s.vms) {
      const idx = specIdxOf(vm.demand ?? {});
      const key = `${vm.node_role}|${vm.ip_type}|${idx}`;
      const g = groups.get(key);
      if (g) g.count += 1;
      else groups.set(key, {
        role: vm.node_role || "worker",
        ipType: vm.ip_type || "routable",
        specIdx: idx,
        count: 1,
      });
    }
    return { name: s.name || "", groups: [...groups.values()] };
  });

  const fleet = deriveFleet(req.baremetals);
  if (!fleet) return false;   // topology the generator cannot reproduce

  const saved = { specs: state.specs, steps: state.steps, cfg: state.cfg };
  state.specs = specs;
  state.steps = steps;
  state.fleet = fleet;
  state.cfg = {
    autoAA: req.config?.auto_generate_anti_affinity ?? true,
    maxSolve: req.config?.max_solve_time_seconds ?? 10,
  };
  // The generator must round-trip the fleet exactly, or the form would
  // silently redefine the user's topology on the next Simulate.
  if (!sameFleet(buildFleet(fleet.count), req.baremetals)) {
    state.specs = saved.specs;
    state.steps = saved.steps;
    state.cfg = saved.cfg;
    return false;
  }
  renderForm();
  return true;
}

const topoKey = (t = {}) =>
  [t.site, t.phase, t.datacenter, t.room, t.rack, t.ag].join("|");

/** Machine model + topology counts implied by an existing fleet. */
function deriveFleet(bms) {
  if (!bms.length) return null;
  const cap = bms[0].total_capacity ?? {};
  const key = specKey(cap);
  // One homogeneous model only — mixed fleets stay in JSON mode.
  if (bms.some((b) => specKey(b.total_capacity ?? {}) !== key)) return null;
  if (bms.some((b) => (b.used_capacity?.cpu_cores ?? 0) !== 0)) return null;
  const distinct = (f) => new Set(bms.map((b) => b.topology?.[f])).size;
  return {
    count: bms.length,
    cpu: cap.cpu_cores ?? 0,
    memGiB: Math.round((cap.memory_mib ?? 0) / GiB),
    disk: cap.storage_gb ?? 0,
    gpu: cap.gpu_count ?? 0,
    sites: distinct("site"), phases: distinct("phase"),
    datacenters: distinct("datacenter"), rooms: distinct("room"),
    racks: distinct("rack"), ags: distinct("ag"),
  };
}

/** Same machines in the same places (ids and hostnames may differ). */
function sameFleet(a, b) {
  if (a.length !== b.length) return false;
  const tally = (list) => {
    const m = new Map();
    for (const bm of list) {
      const k = `${specKey(bm.total_capacity ?? {})}@${topoKey(bm.topology)}`;
      m.set(k, (m.get(k) ?? 0) + 1);
    }
    return m;
  };
  const ma = tally(a);
  const mb = tally(b);
  if (ma.size !== mb.size) return false;
  for (const [k, v] of ma) if (mb.get(k) !== v) return false;
  return true;
}

/* ── seed + wiring ─────────────────────────────────────────────────── */

function seed() {
  state.specs = [
    { name: "M-8c32g", cpu: 8, memGiB: 32, disk: 200, gpu: 0 },
    { name: "L-16c64g", cpu: 16, memGiB: 64, disk: 400, gpu: 0 },
  ];
  state.steps = [
    { name: "cluster-a", groups: [
      { role: "master", ipType: "routable", specIdx: 0, count: 3 },
      { role: "worker", ipType: "routable", specIdx: 1, count: 2 },
    ] },
    { name: "cluster-b", groups: [
      { role: "master", ipType: "routable", specIdx: 0, count: 3 },
      { role: "worker", ipType: "routable", specIdx: 1, count: 4 },
    ] },
  ];
  // Three AGs by default: with a single AG, spread constraints have no
  // buckets to work with and the demo would look artificially packed.
  state.fleet = {
    count: 6, cpu: 64, memGiB: 256, disk: 2000, gpu: 0,
    sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 3, ags: 3,
  };
}

export function initForm(onChange) {
  seed();
  renderForm();

  const root = document.querySelector("#builder");
  root.addEventListener("input", (e) => {
    const el = e.target;
    if (el.id === "cfg-auto-aa") { state.cfg.autoAA = el.checked; onChange?.(); return; }
    if (el.id === "cfg-max-solve") { state.cfg.maxSolve = Number(el.value) || 10; onChange?.(); return; }
    if (el.dataset?.fleet) {
      // "How many" is deliberately allowed to be empty — that is the
      // request to size the fleet instead of fixing it.
      const raw = el.value.trim();
      state.fleet[el.dataset.fleet] = raw === "" ? null : Number(raw);
      renderSummary();
      onChange?.();
      return;
    }
    if (!el.dataset?.s) return;
    applyFieldEdit(el);
    onChange?.();
  });
  root.addEventListener("change", (e) => {
    const el = e.target;
    if (el.dataset?.s && el.tagName === "SELECT") { applyFieldEdit(el); onChange?.(); }
  });
  root.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const stepIdx = btn.dataset.step !== undefined ? Number(btn.dataset.step) : undefined;
    if (handleAction(btn.dataset.act, Number(btn.dataset.i ?? -1), stepIdx)) onChange?.();
  });
}
