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
// Advisory suggestion catalogs only — node_role and ip_type are OPEN
// strings (ADR-010): type anything into the combo inputs below.
const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];
const IP_TYPES = ["routable", "non-routable"];

const combo = (sec, i, f, v, label, listId) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="text" list="${listId}" value="${esc(v ?? "")}"
      pattern="[\\w.-]+" ${bind(sec, i, f)}></label>`;

export function renderDatalists(root) {
  const dl = (id, values) =>
    `<datalist id="${id}">${values.map((x) => `<option value="${esc(x)}">`).join("")}</datalist>`;
  root.insertAdjacentHTML("beforeend",
    dl("role-options", ROLES) + dl("ip-options", IP_TYPES));
}

export const state = {
  specs: [],   // {name, cpu, memGiB, disk, gpu}
  // groups: {role, ipType, specIdx, count, maxPerBm|null, shared, exclusive}
  steps: [],
  // The fleet: one shared topology skeleton + one or more machine models.
  // A model's `roles` list makes it a dedicated pool (only those roles may
  // land on its machines — mockgen's BmProfile.roles semantics); blank =
  // usable by every role. `count` blank on a SINGLE model = sizing mode.
  fleet: {
    topo: { sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 3, ags: 3 },
    models: [
      { count: 6, cpu: 64, memGiB: 256, disk: 2000, gpu: 0, roles: [] },
    ],
  },
  // buildMode "sequential" replays steps in order (the rollout);
  // "parallel" merges every cluster into ONE joint step — the same
  // semantics as the Topology page's single PlacementRequest.
  cfg: { autoAA: true, maxSolve: 10, spreadAg: 3, failover: false,
         buildMode: "sequential" },
};

const blank = {
  spec: () => ({ name: "", cpu: 8, memGiB: 32, disk: 200, gpu: 0 }),
  fmodel: () => ({ count: 3, cpu: 64, memGiB: 256, disk: 2000, gpu: 0, roles: [] }),
  group: () => ({ role: "worker", ipType: "routable", specIdx: 0, count: 3,
                  maxPerBm: null, shared: false, exclusive: false }),
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
  const checkbox = (f, v, label, title) =>
    `<label class="mini mini--check" title="${esc(title)}">
      <input type="checkbox" ${v ? "checked" : ""} ${bind(`groups.${si}`, gi, f)}>
      <span class="mini__label">${esc(label)}</span>
    </label>`;
  return `<div class="group-row">
    <div class="frow frow--group-top">
      ${combo(`groups.${si}`, gi, "role", g.role, "Role", "role-options")}
      ${combo(`groups.${si}`, gi, "ipType", g.ipType, "IP type", "ip-options")}
      ${num(`groups.${si}`, gi, "count", g.count, "Count", 1)}
      ${act("group-dup", gi, "⧉", "Duplicate this group", "", extra)}
      ${act("group-del", gi, "✕", "Delete group", "row-act--del", extra)}
    </div>
    <div class="frow frow--group-bot">
      ${pick(`groups.${si}`, gi, "specIdx", g.specIdx, "VM spec",
        specOpts.length ? specOpts : [{ value: 0, label: "— add a spec first —" }])}
      <label class="mini"><span class="mini__label">max/BM</span>
        <input class="input" type="number" min="1" value="${g.maxPerBm ?? ""}"
          placeholder="∞" ${bind(`groups.${si}`, gi, "maxPerBm")}></label>
      ${checkbox("shared", g.shared, "sh",
        "Shared eco-system group: cluster_id becomes \"shared\" so the group spans clusters")}
      ${checkbox("exclusive", g.exclusive, "ex",
        "Exclusive occupancy (C6): each member owns its machine outright")}
    </div>
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

const topoNum = (f, v, label) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="number" min="1" value="${v ?? ""}"
      placeholder="1" data-fleet="${f}"></label>`;

const modelNum = (i, f, v, label, ph = "") =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="number" min="1" value="${v ?? ""}"
      placeholder="${esc(ph)}" ${bind("fmodels", i, f)}></label>`;

function modelRow(m, i) {
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${modelNum(i, "count", m.count, "How many", "auto")}
      ${modelNum(i, "cpu", m.cpu, "vCore")}
      ${modelNum(i, "memGiB", m.memGiB, "Mem GiB")}
      ${modelNum(i, "disk", m.disk, "Disk GB")}
      ${act("fmodel-del", i, "✕", "Delete model", "row-act--del")}
    </div>
    <label class="mini"><span class="mini__label">Roles (comma — blank = all roles)</span>
      <input class="input" type="text" placeholder="all roles"
        value="${esc((m.roles || []).join(", "))}"
        title="Dedicated pool: only these node roles may land on this model's machines"
        ${bind("fmodels", i, "roles")}></label>
  </div>`;
}

function fleetCard() {
  const f = state.fleet;
  return `
    ${f.models.map(modelRow).join("")}
    <button type="button" class="btn btn--add" data-act="fmodel-add">+ BM model</button>
    <div class="frow-card" style="margin-top:8px">
      <div class="frow frow--topo">
        ${TOPO_DIMS.map(([k, label]) => topoNum(k, f.topo[k], label)).join("")}
      </div>
      <p class="form-empty">
        One topology skeleton for the whole fleet: racks fan out over the
        dimensions, and each model's machines round-robin over the racks
        independently so every pool spans the AGs. With a single model,
        leave <b>How many</b> blank to estimate the minimum fleet.
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
  $("#cfg-spread-ag").value = state.cfg.spreadAg;
  $("#cfg-failover").checked = state.cfg.failover;
  const mode = document.querySelector(
    `input[name="build-mode"][value="${state.cfg.buildMode}"]`);
  if (mode) mode.checked = true;
  renderSummary();
}

function renderSummary() {
  const vms = state.steps.reduce(
    (n, st) => n + st.groups.reduce((m, g) => m + Math.max(0, g.count), 0), 0);
  const fleet = isSizingMode()
    ? "fleet size: estimate"
    : `${fleetCount()} baremetal(s)`;
  const mode = state.cfg.buildMode === "parallel" ? " · all at once" : "";
  $("#builder-summary").textContent =
    `${state.steps.length} step(s) · ${vms} VM(s) · ${fleet}${mode}`;
}

/** Blank/zero machine count on the SINGLE model means "work out the
 * minimum for me". With several models the count is never optional —
 * mixed-fleet sizing is the capacity planner's job, not the rollout
 * sizer's (ADR-014 keeps its search single-model). */
export function isSizingMode() {
  const ms = state.fleet.models;
  if (ms.length !== 1) return false;
  const c = ms[0].count;
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
    if (f === "maxPerBm") g[f] = el.value.trim() === "" ? null : Number(value);
    else if (f === "specIdx" || f === "count") g[f] = Number(value);
    else g[f] = value;
  } else if (sec === "fmodels") {
    const m = state.fleet.models[i];
    if (!m) return;
    // Blank count = sizing request (single model only, checked at build time).
    if (f === "count") m.count = el.value.trim() === "" ? null : Number(value);
    else if (f === "roles") {
      m.roles = el.value.split(",").map((s) => s.trim()).filter(Boolean);
    } else m[f] = Number(value);
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
    case "fmodel-add": state.fleet.models.push(blank.fmodel()); break;
    case "fmodel-del":
      // Keep at least one model — a fleet with zero models is meaningless.
      if (state.fleet.models.length > 1) state.fleet.models.splice(i, 1);
      break;
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

const modelCount = (m) => Math.max(0, Math.round(Number(m.count) || 0));

export function fleetCount() {
  return state.fleet.models.reduce((n, m) => n + modelCount(m), 0);
}

/**
 * Topology skeleton, one entry per rack. Every dimension above rack is
 * derived by modulo over the rack ordinal, so racks fan out across sites,
 * rooms and AGs simultaneously — the same rule the mock generator uses
 * (app/mockgen.py `_build_racks`). Each rack belongs to exactly one AG.
 */
export function buildRackTopologies() {
  const t = state.fleet.topo;
  const racks = dim(t.racks);
  const out = [];
  for (let r = 0; r < racks; r++) {
    out.push({
      site: `site-${(r % dim(t.sites)) + 1}`,
      phase: `p${(r % dim(t.phases)) + 1}`,
      datacenter: `dc-${(r % dim(t.datacenters)) + 1}`,
      room: `room-${(r % dim(t.rooms)) + 1}`,
      rack: `rack-${r + 1}`,
      ag: `ag-${(r % dim(t.ags)) + 1}`,
    });
  }
  return out;
}

/**
 * The concrete fleet: each model's machines round-robin over the rack
 * skeleton INDEPENDENTLY (their own 0-based counter, not a global one),
 * so every pool spans the racks — and therefore the AGs — on its own.
 * A global counter would let a later pool start mid-cycle and end up
 * concentrated in a subset of AGs (mockgen's per-profile convention,
 * app/mockgen.py profile loop). Returns [{bm, roles}] — `roles` is the
 * model's pool restriction ([] = any role may land there).
 */
function buildFleetEntries() {
  const racks = buildRackTopologies();
  const out = [];
  let seq = 0;
  for (const m of state.fleet.models) {
    const n = modelCount(m);
    const roles = (m.roles ?? []).map((r) => String(r).trim()).filter(Boolean);
    for (let i = 0; i < n; i++) {
      seq += 1;
      const s = String(seq).padStart(3, "0");
      out.push({
        bm: {
          id: `bm-${s}`,
          hostname: `bare-${s}`,
          total_capacity: resources(m.cpu, m.memGiB, m.disk, m.gpu),
          used_capacity: resources(0, 0, 0),
          topology: { ...racks[i % racks.length] },
        },
        roles,
      });
    }
  }
  return out;
}

export function buildFleet() {
  return buildFleetEntries().map((e) => e.bm);
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
  const bad = state.fleet.models.findIndex((m) => modelCount(m) < 1);
  if (bad >= 0) {
    throw new Error(`BM model ${bad + 1} needs a machine count — ` +
      "leaving it blank only estimates the fleet when there is a single model.");
  }
  return buildRequestWith({ withCandidates: true });
}

function buildRequestWith({ withCandidates }) {
  if (!state.specs.length) throw new Error("Add at least one VM spec.");
  if (!state.steps.length) throw new Error("Add at least one build step.");
  const entries = withCandidates ? buildFleetEntries() : [];
  const baremetals = entries.map((e) => e.bm);
  if (withCandidates && !baremetals.length) {
    throw new Error("The fleet has zero baremetals.");
  }
  const allBmIds = baremetals.map((b) => b.id);
  // Pool restriction: a VM of role r may land on machines of models whose
  // roles list is empty (general purpose) or contains r. When no model
  // declares a pool the whole fleet is eligible.
  const pooled = entries.some((e) => e.roles.length);
  const candidatesFor = (role) => {
    if (!pooled) return allBmIds;
    const ids = entries
      .filter((e) => !e.roles.length || e.roles.includes(role))
      .map((e) => e.bm.id);
    if (!ids.length) {
      throw new Error(`No BM model accepts role "${role}" — ` +
        "add it to a model's Roles list or clear the pools.");
    }
    return ids;
  };

  const names = new Set();
  // Selector-form rules are emitted once, in the FIRST step where a group
  // key appears: rules accumulate across steps (union), so re-adding an
  // identical shared-group rule every step would only duplicate constraints.
  const emittedRules = new Set();
  const steps = state.steps.map((st, si) => {
    const name = (st.name || "").trim();
    if (!name) throw new Error(`Step ${si + 1} needs a name.`);
    if (names.has(name)) throw new Error(`Duplicate step name "${name}".`);
    names.add(name);
    if (!st.groups.length) throw new Error(`Step "${name}" has no VM groups.`);

    const vms = [];
    const maxPerBmRules = [];
    const exclusiveRules = [];
    const failoverRules = [];
    const roleCounts = {};
    st.groups.forEach((g, gi) => {
      const spec = state.specs[g.specIdx];
      if (!spec) throw new Error(`Step "${name}" group ${gi + 1} has no valid spec.`);
      // Shared eco-system groups live under cluster_id "shared" so the
      // auto anti-affinity / C6 group spans clusters (ADR-011); ids keep
      // the step prefix for rollout-wide uniqueness.
      const cluster = g.shared ? "shared" : name;
      if (!g.shared) roleCounts[g.role] = (roleCounts[g.role] ?? 0) + Math.max(0, g.count);
      const count = Math.max(0, Math.round(g.count));
      for (let k = 0; k < count; k++) {
        vms.push({
          id: `${name}-${g.role}-${gi + 1}-${k + 1}`,
          demand: resources(spec.cpu, spec.memGiB, spec.disk, spec.gpu),
          node_role: g.role,
          ip_type: g.ipType,
          cluster_id: cluster,
          ...(withCandidates ? { candidate_baremetals: candidatesFor(g.role) } : {}),
        });
      }
      const selector = { cluster_id: cluster, ip_type: g.ipType, node_role: g.role };
      if (g.maxPerBm != null && g.maxPerBm >= 1) {
        const id = `maxbm/${cluster}/${g.ipType}/${g.role}`;
        if (!emittedRules.has(id)) {
          emittedRules.add(id);
          maxPerBmRules.push({ group_id: id, selector, max_per_bm: Math.round(g.maxPerBm) });
        }
      }
      if (g.exclusive) {
        const id = `excl/${cluster}/${g.ipType}/${g.role}`;
        if (!emittedRules.has(id)) {
          emittedRules.add(id);
          exclusiveRules.push({ group_id: id, selector });
        }
      }
    });
    // Failover follows the mockgen convention: per cluster, masters backed
    // by learners of the SAME cluster, N-1 over AGs; skipped when either
    // role is absent (the backup selector would resolve empty).
    if (state.cfg.failover && (roleCounts.master ?? 0) >= 1 && (roleCounts.learner ?? 0) >= 1) {
      failoverRules.push({
        rule_id: `auto-failover-${name}`,
        primary: { cluster_id: name, node_role: "master" },
        backup: { cluster_id: name, node_role: "learner" },
        fault_domain: "ag",
      });
    }
    if (!vms.length) throw new Error(`Step "${name}" produces zero VMs (counts are 0).`);
    const step = { name, vms };
    if (maxPerBmRules.length) step.max_per_bm_rules = maxPerBmRules;
    if (exclusiveRules.length) step.exclusive_bm_rules = exclusiveRules;
    if (failoverRules.length) step.failover_rules = failoverRules;
    return step;
  });

  // Parallel mode: one joint step — every cluster placed at once, exactly
  // what the Topology page's single PlacementRequest does. Useful both on
  // its own and as the baseline to compare the sequential run against.
  const finalSteps = state.cfg.buildMode === "parallel"
    ? [{
        name: "all-clusters",
        vms: steps.flatMap((st) => st.vms),
        max_per_bm_rules: steps.flatMap((st) => st.max_per_bm_rules ?? []),
        exclusive_bm_rules: steps.flatMap((st) => st.exclusive_bm_rules ?? []),
        failover_rules: steps.flatMap((st) => st.failover_rules ?? []),
      }]
    : steps;

  return {
    baremetals,
    steps: finalSteps,
    config: {
      auto_generate_anti_affinity: !!state.cfg.autoAA,
      max_solve_time_seconds: Math.max(1, Number(state.cfg.maxSolve) || 10),
      target_spread: { ag: Math.max(1, Math.round(Number(state.cfg.spreadAg) || 3)) },
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
  const ms = state.fleet.models;
  if (ms.length !== 1) {
    throw new Error("Sizing works on a single BM model — " +
      "give every model a count, or delete the extra models.");
  }
  if ((ms[0].roles ?? []).some((r) => String(r).trim())) {
    throw new Error("Sizing doesn't support role pools — " +
      "clear the model's Roles list to estimate the fleet.");
  }
  const req = buildRequestWith({ withCandidates: false });
  const m = ms[0];
  const t = state.fleet.topo;
  return {
    fleet: {
      total_capacity: resources(m.cpu, m.memGiB, m.disk, m.gpu),
      sites: dim(t.sites), phases: dim(t.phases),
      datacenters: dim(t.datacenters), rooms: dim(t.rooms),
      racks: dim(t.racks), ags: dim(t.ags),
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
  // Only selector-form rules following the form's own naming conventions
  // (maxbm/…, excl/…, auto-failover-…) can round-trip; anything else —
  // vm_ids rules, explicit anti-affinity, coarse requirements, brownfield
  // existing_vms — stays in JSON mode.
  const expressible = req.steps.every(
    (s) => (s.vms?.length ?? 0) > 0 &&
           !(s.requirements?.length) && !(s.anti_affinity_rules?.length),
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
    // Collapse VMs into groups keyed by (role, ip_type, spec, shared).
    const groups = new Map();
    for (const vm of s.vms) {
      const idx = specIdxOf(vm.demand ?? {});
      const shared = vm.cluster_id === "shared";
      if (!shared && vm.cluster_id && vm.cluster_id !== s.name) return null;
      const key = `${vm.node_role}|${vm.ip_type}|${idx}|${shared}`;
      const g = groups.get(key);
      if (g) g.count += 1;
      else groups.set(key, {
        role: vm.node_role || "worker",
        ipType: vm.ip_type || "routable",
        specIdx: idx,
        count: 1,
        maxPerBm: null,
        shared,
        exclusive: false,
      });
    }
    return { name: s.name || "", groups: [...groups.values()] };
  });
  if (steps.some((s) => s === null)) return false;

  // Rules are emitted once, in the first step a group key appears, but a
  // shared group may recur in later steps — so match every rule against
  // ALL steps' groups. An unmatched or non-conventional rule → JSON mode.
  const groupsFor = (sel, stepName) => {
    const wantShared = sel?.cluster_id === "shared";
    if (!wantShared && sel?.cluster_id !== stepName) return null;
    const hits = [];
    for (const st of steps) {
      if (!wantShared && st.name !== stepName) continue;
      for (const g of st.groups) {
        if (g.shared === wantShared && g.role === sel?.node_role &&
            g.ipType === sel?.ip_type) hits.push(g);
      }
    }
    return hits.length ? hits : null;
  };
  let failover = false;
  for (const st of req.steps) {
    for (const rule of st.max_per_bm_rules ?? []) {
      if (rule.vm_ids?.length || !rule.selector) return false;
      const hits = groupsFor(rule.selector, st.name);
      if (!hits || !(rule.max_per_bm >= 1)) return false;
      hits.forEach((g) => { g.maxPerBm = rule.max_per_bm; });
    }
    for (const rule of st.exclusive_bm_rules ?? []) {
      if (rule.vm_ids?.length || !rule.selector) return false;
      const hits = groupsFor(rule.selector, st.name);
      if (!hits) return false;
      hits.forEach((g) => { g.exclusive = true; });
    }
    for (const rule of st.failover_rules ?? []) {
      const conventional =
        rule.primary?.cluster_id === st.name &&
        rule.primary?.node_role === "master" &&
        rule.backup?.cluster_id === st.name &&
        rule.backup?.node_role === "learner" &&
        rule.fault_domain === "ag";
      if (!conventional) return false;
      failover = true;
    }
  }

  const fleet = deriveFleet(
    req.baremetals, req.steps.flatMap((s) => s.vms ?? []));
  if (!fleet) return false;   // topology/pools the generator cannot reproduce

  const saved = { specs: state.specs, steps: state.steps,
                  fleet: state.fleet, cfg: state.cfg };
  state.specs = specs;
  state.steps = steps;
  state.fleet = fleet;
  state.cfg = {
    autoAA: req.config?.auto_generate_anti_affinity ?? true,
    maxSolve: req.config?.max_solve_time_seconds ?? 10,
    spreadAg: req.config?.target_spread?.ag ?? 3,
    failover,
    buildMode: (req.steps.length === 1 && req.steps[0].name === "all-clusters")
      ? "parallel" : "sequential",
  };
  // The generator must round-trip the fleet exactly, or the form would
  // silently redefine the user's topology on the next Simulate.
  if (!sameFleet(buildFleet(), req.baremetals)) {
    state.specs = saved.specs;
    state.steps = saved.steps;
    state.fleet = saved.fleet;
    state.cfg = saved.cfg;
    return false;
  }
  renderForm();
  return true;
}

const topoKey = (t = {}) =>
  [t.site, t.phase, t.datacenter, t.room, t.rack, t.ag].join("|");

/**
 * Machine models + topology counts implied by an existing fleet, plus the
 * pool restriction each model must carry to reproduce the request's
 * candidate lists. Machines are grouped into models by capacity
 * (first-appearance order); pools are inferred from the VMs' candidate
 * sets:
 *   - all VMs of one role must share ONE candidate set (the form emits
 *     candidates per role, never per VM),
 *   - each model must sit fully inside or fully outside every role's set
 *     (a half-included model has no roles-list encoding),
 *   - a model inside every role's set is general purpose (roles: []),
 *     otherwise its roles list is exactly the roles that include it.
 * A final replay check confirms the inferred pools regenerate every
 * candidate set verbatim; anything that doesn't stays in JSON mode.
 */
function deriveFleet(bms, vms) {
  if (!bms.length) return null;
  if (bms.some((b) => (b.used_capacity?.cpu_cores ?? 0) !== 0)) return null;

  const models = [];
  const byCap = new Map();
  for (const b of bms) {
    const key = specKey(b.total_capacity ?? {});
    if (!byCap.has(key)) {
      const c = b.total_capacity ?? {};
      byCap.set(key, models.length);
      models.push({
        count: 0,
        cpu: c.cpu_cores ?? 0,
        memGiB: Math.round((c.memory_mib ?? 0) / GiB),
        disk: c.storage_gb ?? 0,
        gpu: c.gpu_count ?? 0,
        roles: [],
        ids: new Set(),
      });
    }
    const m = models[byCap.get(key)];
    m.count += 1;
    m.ids.add(b.id);
  }

  // One candidate set per role. A VM without candidate_baremetals means
  // "anywhere" (the server treats a missing list as unrestricted).
  const allIds = new Set(bms.map((b) => b.id));
  const setKey = (s) => [...s].sort().join("\n");
  const byRole = new Map();
  for (const vm of vms) {
    const set = vm.candidate_baremetals?.length
      ? new Set(vm.candidate_baremetals) : allIds;
    const role = vm.node_role || "worker";
    const prev = byRole.get(role);
    if (prev) {
      if (prev.key !== setKey(set)) return null;
    } else {
      byRole.set(role, { key: setKey(set), set });
    }
  }

  for (const m of models) {
    let inAll = true;
    const accepts = [];
    for (const [role, { set }] of byRole) {
      let inside = 0;
      for (const id of m.ids) if (set.has(id)) inside += 1;
      if (inside !== 0 && inside !== m.ids.size) return null;
      if (inside === m.ids.size) accepts.push(role);
      else inAll = false;
    }
    m.roles = inAll ? [] : accepts;
  }
  // Replay: the inferred pools must reproduce each role's candidate set.
  for (const [role, { set }] of byRole) {
    const expected = new Set();
    for (const m of models) {
      if (!m.roles.length || m.roles.includes(role)) {
        for (const id of m.ids) expected.add(id);
      }
    }
    if (expected.size !== set.size) return null;
    for (const id of expected) if (!set.has(id)) return null;
  }

  const distinct = (f) => new Set(bms.map((b) => b.topology?.[f])).size;
  return {
    topo: {
      sites: distinct("site"), phases: distinct("phase"),
      datacenters: distinct("datacenter"), rooms: distinct("room"),
      racks: distinct("rack"), ags: distinct("ag"),
    },
    models: models.map(({ ids, ...m }) => m),
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
    topo: { sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 3, ags: 3 },
    models: [
      { count: 6, cpu: 64, memGiB: 256, disk: 2000, gpu: 0, roles: [] },
    ],
  };
}

export function initForm(onChange) {
  seed();

  const root = document.querySelector("#builder");
  // Suggestion datalists live directly under #builder, outside the row
  // containers renderForm() rewrites, so they survive re-renders.
  renderDatalists(root);
  renderForm();
  root.addEventListener("input", (e) => {
    const el = e.target;
    if (el.id === "cfg-auto-aa") { state.cfg.autoAA = el.checked; onChange?.(); return; }
    if (el.id === "cfg-max-solve") { state.cfg.maxSolve = Number(el.value) || 10; onChange?.(); return; }
    if (el.id === "cfg-spread-ag") { state.cfg.spreadAg = Number(el.value) || 3; onChange?.(); return; }
    if (el.id === "cfg-failover") { state.cfg.failover = el.checked; onChange?.(); return; }
    if (el.name === "build-mode") { state.cfg.buildMode = el.value; renderSummary(); onChange?.(); return; }
    if (el.dataset?.fleet) {
      // Topology knobs only; machine counts live on the model rows.
      state.fleet.topo[el.dataset.fleet] = Number(el.value) || 1;
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
