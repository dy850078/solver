/* Rollout builder form.
 *
 * Turns three small tables into a RolloutRequest:
 *   1. a named VM-spec catalog (define a size once, reuse it everywhere),
 *   2. an ordered list of build steps (one per cluster), each holding
 *      VM groups = role × ip_type × spec × count,
 *   3. a baremetal stock table (count × model × placement).
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
  stock: [],   // {count, cpu, memGiB, disk, ag}
  cfg: { autoAA: true, maxSolve: 10 },
};

const blank = {
  spec: () => ({ name: "", cpu: 8, memGiB: 32, disk: 200, gpu: 0 }),
  group: () => ({ role: "worker", ipType: "routable", specIdx: 0, count: 3 }),
  stock: () => ({ count: 3, cpu: 64, memGiB: 256, disk: 2000, ag: "ag-1" }),
};

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

function stockRow(s, i) {
  return `<div class="frow-card"><div class="frow frow--stock">
    ${num("stock", i, "count", s.count, "How many", 1)}
    ${num("stock", i, "cpu", s.cpu, "vCore")}
    ${num("stock", i, "memGiB", s.memGiB, "Mem GiB")}
    ${num("stock", i, "disk", s.disk, "Disk GB")}
    ${txt("stock", i, "ag", s.ag, "AG", "ag-1")}
    ${act("stock-del", i, "✕", "Delete row", "row-act--del")}
  </div></div>`;
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
  $("#stock-rows").innerHTML = state.stock.length
    ? state.stock.map(stockRow).join("")
    : `<p class="form-empty">No baremetals yet — add stock for the simulation.</p>`;
  $("#cfg-auto-aa").checked = state.cfg.autoAA;
  $("#cfg-max-solve").value = state.cfg.maxSolve;
  renderSummary();
}

function renderSummary() {
  const bms = state.stock.reduce((n, s) => n + Math.max(0, s.count), 0);
  const vms = state.steps.reduce(
    (n, st) => n + st.groups.reduce((m, g) => m + Math.max(0, g.count), 0), 0);
  $("#builder-summary").textContent =
    `${state.steps.length} step(s) · ${vms} VM(s) · ${bms} baremetal(s)`;
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
    case "stock-add": state.stock.push(blank.stock()); break;
    case "stock-del": state.stock.splice(i, 1); break;
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

/**
 * Assemble a RolloutRequest. Throws Error with a human message when the
 * form cannot produce a runnable request.
 */
export function buildRequest() {
  if (!state.specs.length) throw new Error("Add at least one VM spec.");
  if (!state.steps.length) throw new Error("Add at least one build step.");
  if (!state.stock.length) throw new Error("Add at least one baremetal stock row.");

  const baremetals = [];
  let n = 0;
  state.stock.forEach((row) => {
    for (let k = 0; k < Math.max(0, row.count); k++) {
      n += 1;
      const id = `bm-${String(n).padStart(2, "0")}`;
      baremetals.push({
        id,
        hostname: `bare-${String(n).padStart(3, "0")}`,
        total_capacity: resources(row.cpu, row.memGiB, row.disk),
        used_capacity: resources(0, 0, 0),
        topology: {
          site: "site-a", phase: "p1", datacenter: "dc-1",
          room: "room-1", rack: `rack-${n}`, ag: row.ag || "ag-1",
        },
      });
    }
  });
  if (!baremetals.length) throw new Error("Stock rows produce zero baremetals.");
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
          candidate_baremetals: allBmIds,
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

  // Collapse baremetals into stock rows keyed by (capacity, ag).
  const stock = new Map();
  for (const bm of req.baremetals) {
    const cap = bm.total_capacity ?? {};
    const ag = bm.topology?.ag ?? "ag-1";
    const key = `${specKey(cap)}|${ag}`;
    const row = stock.get(key);
    if (row) row.count += 1;
    else stock.set(key, {
      count: 1,
      cpu: cap.cpu_cores ?? 0,
      memGiB: Math.round((cap.memory_mib ?? 0) / GiB),
      disk: cap.storage_gb ?? 0,
      ag,
    });
  }

  state.specs = specs;
  state.steps = steps;
  state.stock = [...stock.values()];
  state.cfg = {
    autoAA: req.config?.auto_generate_anti_affinity ?? true,
    maxSolve: req.config?.max_solve_time_seconds ?? 10,
  };
  renderForm();
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
  state.stock = [
    { count: 2, cpu: 64, memGiB: 256, disk: 2000, ag: "ag-1" },
    { count: 2, cpu: 64, memGiB: 256, disk: 2000, ag: "ag-2" },
    { count: 2, cpu: 64, memGiB: 256, disk: 2000, ag: "ag-3" },
  ];
}

export function initForm(onChange) {
  seed();
  renderForm();

  const root = document.querySelector("#builder");
  root.addEventListener("input", (e) => {
    const el = e.target;
    if (el.id === "cfg-auto-aa") { state.cfg.autoAA = el.checked; onChange?.(); return; }
    if (el.id === "cfg-max-solve") { state.cfg.maxSolve = Number(el.value) || 10; onChange?.(); return; }
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
