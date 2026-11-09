/* PM Demand builder form.
 *
 * Builds a BmDemandPlanRequest (app/bm_demand.py, ADR-020) from six small
 * tables and turns one back into the form, losslessly — the saved
 * "template" IS the request JSON (plus `_name` / `_description`, stripped
 * before POST), so anything the form writes can be curl'd as-is.
 *
 *   1. machine models    (name + usable capacity)
 *   2. VM specs          (name + size; "derive" = model ÷ k, the PM's
 *                         whole / half / third of a machine)
 *   3. cluster demands   (cluster × node type × ip → spec, and EITHER
 *                         "N equivalent machines" OR "count VMs")
 *   4. tenancy groups    (clusters that may share machines; each group is
 *                         its own pool on one machine model; blank fleet
 *                         size = "how many?", a number = "does it fit?")
 *   5. policy            (exclusive clusters, default footprint ≥ N
 *                         machines, topology)
 *   6. build order       (step names; a demand's step defaults to its
 *                         cluster id)
 *
 * Same conventions as rollout-form.js: template strings, every input bound
 * by data-s / data-i / data-f, delegated listeners under #builder, full
 * re-render on structural edits. Memory is entered in GiB and stored as
 * memory_mib (×1024).
 */

import { escapeHtml as esc, formatGpu, parseGpu } from "./util.js";

const GiB = 1024;
const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];
const IP_OPTIONS = [
  { value: "routable", label: "R · routable" },
  { value: "non-routable", label: "NR · non-routable" },
  { value: "", label: "— (any)" },
];

export const state = {
  name: "",
  description: "",
  bmSpecs: [],   // {name, cpu, memGiB, disk, gpu:{model:n}}
  vmSpecs: [],   // same shape
  // {cluster, role, ip, spec(name), basis:"bm"|"vm", n, minDistinct:null|int, step:""}
  demands: [],
  // {name, clusters:[...], bmSpec(name), fleetSize:null|int}
  groups: [],
  exclusive: [],  // cluster ids
  policy: { defaultMinDistinct: 6 },
  topo: { sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 6, ags: 3 },
  buildOrder: [], // step names, reconciled against demands on every edit
  cfg: { maxSolve: 10, autoAA: true, wHeadroom: 0, maxBaremetals: 200, maxProbes: 12, deadline: 120 },
};

const blank = {
  bm: () => ({ name: `bm-${state.bmSpecs.length + 1}`, cpu: 90, memGiB: 720, disk: 6000, gpu: {} }),
  vm: () => ({ name: `vm-${state.vmSpecs.length + 1}`, cpu: 45, memGiB: 360, disk: 3000, gpu: {} }),
  demand: () => ({
    cluster: `cluster-${state.demands.length + 1}`, role: "worker", ip: "routable",
    spec: state.vmSpecs[0]?.name ?? "", basis: "bm", n: 5, minDistinct: null, step: "",
  }),
  group: () => ({ name: `pool-${state.groups.length + 1}`, clusters: [], bmSpec: state.bmSpecs[0]?.name ?? "", fleetSize: null }),
};

const TOPO_DIMS = [
  ["sites", "Sites"], ["phases", "Phases"], ["datacenters", "DCs"],
  ["rooms", "Rooms"], ["racks", "Racks"], ["ags", "AGs"],
];

/* ── pure helpers (mirror app/bm_demand.py) ───────────────────────── */

/** min over demanded dimensions of floor(cap/spec); 0 = does not fit. */
export function fitsPerBm(spec, cap) {
  let k = null;
  const dims = [["cpu", spec.cpu, cap.cpu], ["memGiB", spec.memGiB, cap.memGiB], ["disk", spec.disk, cap.disk]];
  for (const m of new Set([...Object.keys(spec.gpu || {}), ...Object.keys(cap.gpu || {})])) {
    dims.push([`gpu:${m}`, spec.gpu?.[m] ?? 0, cap.gpu?.[m] ?? 0]);
  }
  for (const [, need, have] of dims) {
    if (!(need > 0)) continue;
    const fit = Math.floor((have ?? 0) / need);
    k = k === null ? fit : Math.min(k, fit);
  }
  return k === null ? 0 : k;
}

const ceilDiv = (a, b) => Math.ceil(a / b);

export function clusterNames() {
  return [...new Set(state.demands.map((d) => d.cluster.trim()).filter(Boolean))];
}

function stepNames() {
  return [...new Set(state.demands.map((d) => (d.step || d.cluster).trim()).filter(Boolean))];
}

function groupOf(cluster) {
  return state.groups.find((g) => g.clusters.includes(cluster)) ?? null;
}

/** Keep buildOrder = (existing order ∩ current steps) + new steps. */
function reconcileOrder() {
  const names = stepNames();
  const kept = state.buildOrder.filter((n) => names.includes(n));
  for (const n of names) if (!kept.includes(n)) kept.push(n);
  state.buildOrder = kept;
}

/** Cluster rename: follow it in groups / exclusive when no other demand
 * still carries the old name (otherwise both names are live). */
function propagateClusterRename(oldName, newName) {
  if (!oldName || oldName === newName) return;
  if (state.demands.some((d) => d.cluster === oldName)) return;
  for (const g of state.groups) g.clusters = g.clusters.map((c) => (c === oldName ? newName : c));
  state.exclusive = state.exclusive.map((c) => (c === oldName ? newName : c));
}

/** Compile preview for one demand — the same arithmetic the server runs,
 * shown next to the row so "11 machines → 22 VMs" is visible before Run. */
export function previewDemand(d) {
  const spec = state.vmSpecs.find((s) => s.name === d.spec);
  const g = groupOf(d.cluster.trim());
  const bm = state.bmSpecs.find((b) => b.name === (g?.bmSpec ?? state.bmSpecs[0]?.name));
  if (!spec || !bm) return { ok: false, text: spec ? "no machine model" : "pick a spec" };
  const exclusive = state.exclusive.includes(d.cluster.trim());
  const kPhys = fitsPerBm(spec, bm);
  if (kPhys === 0) return { ok: false, text: `'${spec.name}' does not fit '${bm.name}'` };
  const k = exclusive ? 1 : kPhys;
  const n = d.basis === "vm" ? ceilDiv(d.n, k) : d.n;
  const count = d.basis === "vm" ? d.n : d.n * k;
  const minD = d.minDistinct ?? state.policy.defaultMinDistinct ?? 0;
  const warn = [];
  if (minD && count < minD) warn.push(`${count} VM(s) can't span ${minD} machines`);
  else if (minD && n < minD) warn.push(`policy ≥${minD} machines > ${n}`);
  if (state.cfg.autoAA && n < Math.min(count, state.topo.ags)) warn.push(`AG spread needs ${Math.min(count, state.topo.ags)} machines`);
  return { ok: warn.length === 0 || count >= minD, k, n, count, exclusive, warn };
}

/* ── field helpers ─────────────────────────────────────────────────── */

const bind = (sec, i, f, extra = "") => `data-s="${sec}" data-i="${i}" data-f="${f}" ${extra}`;

const txt = (sec, i, f, v, label, ph = "", extra = "") =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="text" value="${esc(v ?? "")}" placeholder="${esc(ph)}" ${bind(sec, i, f, extra)}></label>`;

const combo = (sec, i, f, v, label, listId) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="text" list="${listId}" value="${esc(v ?? "")}" pattern="[\\w.-]+" ${bind(sec, i, f)}></label>`;

const num = (sec, i, f, v, label, { min = 0, ph = "" } = {}) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <input class="input" type="number" min="${min}" value="${v ?? ""}" placeholder="${esc(ph)}" ${bind(sec, i, f)}></label>`;

const pick = (sec, i, f, v, label, options) =>
  `<label class="mini"><span class="mini__label">${esc(label)}</span>
    <select class="select" ${bind(sec, i, f)}>${options.map((o) =>
      `<option value="${esc(o.value)}"${String(o.value) === String(v) ? " selected" : ""}>${esc(o.label)}</option>`,
    ).join("")}</select></label>`;

const act = (action, i, glyph, title, cls = "", extra = "") =>
  `<button type="button" class="row-act ${cls}" data-act="${action}" data-i="${i}" title="${esc(title)}" ${extra}>${glyph}</button>`;

const specOpts = () => state.vmSpecs.map((s) => ({ value: s.name, label: `${s.name} · ${s.cpu}c/${s.memGiB}g` }));
const bmOpts = () => state.bmSpecs.map((b) => ({ value: b.name, label: `${b.name} · ${b.cpu}c/${b.memGiB}g` }));

/* ── row templates ─────────────────────────────────────────────────── */

function catRow(sec, s, i, delAct) {
  return `<div class="frow-card"><div class="frow frow--cat">
    ${txt(sec, i, "name", s.name, "Name")}
    ${num(sec, i, "cpu", s.cpu, "vCore")}
    ${num(sec, i, "memGiB", s.memGiB, "Mem GiB")}
    ${num(sec, i, "disk", s.disk, "Disk GB")}
    ${txt(sec, i, "gpu", formatGpu(s.gpu || {}), "GPU", "h200:8")}
    ${act(delAct, i, "✕", "Delete", "row-act--del")}
  </div></div>`;
}

function demandRow(d, i) {
  const p = previewDemand(d);
  const preview = p.ok === false && p.text
    ? `<span class="warn">${esc(p.text)}</span>`
    : `<span><b>k</b> = ${p.k}/machine${p.exclusive ? " (exclusive)" : ""}</span>
       <span><b>${p.count}</b> VM(s)</span>
       <span><b>${p.n}</b> equivalent machine(s)</span>
       ${(p.warn || []).map((w) => `<span class="warn">⚠ ${esc(w)}</span>`).join("")}`;
  const basis = `<div class="segmented segmented--mini" role="radiogroup">
    <label><input type="radio" name="basis-${i}" value="bm" ${d.basis === "bm" ? "checked" : ""} ${bind("demands", i, "basis")}><span>Machines</span></label>
    <label><input type="radio" name="basis-${i}" value="vm" ${d.basis === "vm" ? "checked" : ""} ${bind("demands", i, "basis")}><span>VMs</span></label>
  </div>`;
  return `<div class="frow-card">
    <div class="frow frow--demand-top">
      ${combo("demands", i, "cluster", d.cluster, "Cluster", "cluster-options")}
      ${combo("demands", i, "role", d.role, "Node type", "role-options")}
      ${pick("demands", i, "ip", d.ip, "IP", IP_OPTIONS)}
      ${act("demand-dup", i, "⧉", "Duplicate")}
      ${act("demand-del", i, "✕", "Delete", "row-act--del")}
    </div>
    <div class="frow frow--demand-bot">
      ${pick("demands", i, "spec", d.spec, "VM spec", specOpts())}
      <label class="mini"><span class="mini__label">Basis</span>${basis}</label>
      ${num("demands", i, "n", d.n, d.basis === "vm" ? "VM count" : "Machines", { min: 1 })}
      ${num("demands", i, "minDistinct", d.minDistinct, "≥ machines", { min: 0, ph: `default ${state.policy.defaultMinDistinct ?? 0}` })}
      ${txt("demands", i, "step", d.step, "Step", d.cluster || "cluster")}
    </div>
    <div class="demand-preview">${preview}</div>
  </div>`;
}

function groupCard(g, i) {
  const all = clusterNames();
  const picks = all.length
    ? all.map((c) => {
      const owner = groupOf(c);
      const taken = owner && owner !== g;
      return `<label class="${taken ? "is-taken" : ""}" title="${taken ? `in group ${esc(owner.name)}` : ""}">
        <input type="checkbox" data-gpick="${i}" data-cluster="${esc(c)}"
          ${g.clusters.includes(c) ? "checked" : ""} ${taken ? "disabled" : ""}>
        <span>${esc(c)}</span></label>`;
    }).join("")
    : `<p class="picks-hint">Add demands first — clusters appear here.</p>`;
  return `<div class="frow-card step-card">
    <div class="frow frow--group-head">
      ${txt("groups", i, "name", g.name, "Group / pool")}
      ${pick("groups", i, "bmSpec", g.bmSpec, "Machine model", bmOpts())}
      ${num("groups", i, "fleetSize", g.fleetSize, "Machines", { min: 1, ph: "size it" })}
      ${act("group-del", i, "✕", "Delete group", "row-act--del")}
    </div>
    <div class="cluster-picks">${picks}</div>
  </div>`;
}

function policyCard() {
  const all = clusterNames();
  const excl = all.length
    ? all.map((c) => `<label>
        <input type="checkbox" data-excl="${esc(c)}" ${state.exclusive.includes(c) ? "checked" : ""}>
        <span>${esc(c)}</span></label>`).join("")
    : `<p class="picks-hint">No clusters yet.</p>`;
  return `<div class="frow-card">
    <div class="frow frow--policy">
      <label class="mini" title="Every node type of every cluster must land on at least this many distinct machines (C7). 0 = off. A demand row can override it.">
        <span class="mini__label">Default footprint ≥ machines</span>
        <input class="input" type="number" min="0" value="${state.policy.defaultMinDistinct ?? 0}" data-policy="defaultMinDistinct"></label>
      <div></div>
    </div>
    <span class="mini__label">Exclusive clusters <span class="muted">— one VM per machine, nobody else on it (C6)</span></span>
    <div class="cluster-picks excl-picks">${excl}</div>
    <span class="mini__label" style="margin-top:4px">Topology <span class="muted">— racks fan out over AGs; counts of 1 collapse that dimension</span></span>
    <div class="frow frow--topo">
      ${TOPO_DIMS.map(([k, label]) => `<label class="mini"><span class="mini__label">${label}</span>
        <input class="input" type="number" min="1" value="${state.topo[k]}" data-topo="${k}"></label>`).join("")}
    </div>
  </div>`;
}

function orderRows() {
  reconcileOrder();
  if (!state.buildOrder.length) return `<p class="form-empty">Steps follow from the demands (one per cluster unless a row names a step).</p>`;
  return state.buildOrder.map((name, i) => {
    const members = state.demands.filter((d) => (d.step || d.cluster).trim() === name);
    const detail = members.map((d) => `${d.cluster}/${d.role}`).join(", ");
    return `<div class="order-row">
      <span class="step-card__seq">${i + 1}</span>
      <span>${esc(name)} <span class="muted">${esc(detail)}</span></span>
      ${act("order-up", i, "↑", "Earlier", "", i === 0 ? "disabled" : "")}
      ${act("order-down", i, "↓", "Later", "", i === state.buildOrder.length - 1 ? "disabled" : "")}
    </div>`;
  }).join("");
}

/* ── render ────────────────────────────────────────────────────────── */

const $ = (sel) => document.querySelector(sel);

function renderDatalists(root) {
  const dl = (id, values) =>
    `<datalist id="${id}">${values.map((x) => `<option value="${esc(x)}">`).join("")}</datalist>`;
  root.insertAdjacentHTML("beforeend", dl("role-options", ROLES) + `<datalist id="cluster-options"></datalist>`);
}

function refreshClusterDatalist() {
  const dl = document.getElementById("cluster-options");
  if (dl) dl.innerHTML = clusterNames().map((c) => `<option value="${esc(c)}">`).join("");
}

export function renderForm() {
  $("#tpl-name").value = state.name;
  $("#bm-rows").innerHTML = state.bmSpecs.length
    ? state.bmSpecs.map((s, i) => catRow("bmSpecs", s, i, "bm-del")).join("")
    : `<p class="form-empty">No machine model yet.</p>`;
  $("#vm-rows").innerHTML = state.vmSpecs.length
    ? state.vmSpecs.map((s, i) => catRow("vmSpecs", s, i, "vm-del")).join("")
    : `<p class="form-empty">No VM spec yet — derive one from a machine model below.</p>`;
  $("#derive-model").innerHTML = bmOpts().map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
  $("#demand-rows").innerHTML = state.demands.length
    ? state.demands.map(demandRow).join("")
    : `<p class="form-empty">No demands yet.</p>`;
  $("#group-rows").innerHTML = state.groups.length
    ? state.groups.map(groupCard).join("")
    : `<p class="form-empty">No groups — every cluster shares one pool on the first machine model.</p>`;
  $("#policy-card").innerHTML = policyCard();
  $("#order-rows").innerHTML = orderRows();
  $("#cfg-max-solve").value = state.cfg.maxSolve;
  $("#cfg-headroom").value = state.cfg.wHeadroom;
  $("#cfg-auto-aa").checked = state.cfg.autoAA;
  $("#cfg-max-bm").value = state.cfg.maxBaremetals;
  $("#cfg-max-probes").value = state.cfg.maxProbes;
  $("#cfg-deadline").value = state.cfg.deadline;
  refreshClusterDatalist();
  renderSummary();
}

function renderSummary() {
  let vms = 0, machines = 0;
  for (const d of state.demands) {
    const p = previewDemand(d);
    if (p.count) { vms += p.count; machines += p.n; }
  }
  const groups = state.groups.length || 1;
  $("#builder-summary").textContent =
    `${state.demands.length} demand(s) · ${clusterNames().length} cluster(s) · ${groups} pool(s) · ≈${vms} VM(s) on ≥${machines} machine(s)`;
}

function renderPreviews() {
  // Cheap refresh of the per-row preview + summary without rebuilding inputs.
  document.querySelectorAll("#demand-rows .frow-card").forEach((card, i) => {
    const d = state.demands[i];
    if (!d) return;
    const tmp = document.createElement("div");
    tmp.innerHTML = demandRow(d, i);
    card.querySelector(".demand-preview").innerHTML = tmp.querySelector(".demand-preview").innerHTML;
  });
  renderSummary();
}

/* ── mutation ──────────────────────────────────────────────────────── */

function coerceNum(el) {
  const n = Number(el.value);
  return Number.isFinite(n) ? n : 0;
}

function applyFieldEdit(el) {
  const sec = el.dataset.s;
  const i = Number(el.dataset.i);
  const f = el.dataset.f;
  const row = state[sec]?.[i];
  if (!row) return "none";
  if (sec === "bmSpecs" || sec === "vmSpecs") {
    if (f === "gpu") row.gpu = parseGpu(el.value);
    else if (f === "name") {
      const old = row.name;
      row.name = el.value.trim();
      // Names are references: follow a rename in demands / groups.
      if (sec === "vmSpecs") for (const d of state.demands) if (d.spec === old) d.spec = row.name;
      if (sec === "bmSpecs") for (const g of state.groups) if (g.bmSpec === old) g.bmSpec = row.name;
      return "rerender";
    } else row[f] = coerceNum(el);
    return "preview";
  }
  if (sec === "demands") {
    if (f === "cluster") {
      const old = row.cluster;
      row.cluster = el.value.trim();
      propagateClusterRename(old, row.cluster);
      return "rerender-soft";
    }
    if (f === "n") row.n = Math.max(1, Math.round(coerceNum(el)));
    else if (f === "minDistinct") row.minDistinct = el.value.trim() === "" ? null : Math.max(0, Math.round(coerceNum(el)));
    else if (f === "basis") { row.basis = el.value; return "rerender"; }
    else if (f === "step") { row.step = el.value.trim(); return "rerender-soft"; }
    else row[f] = el.value;
    return "preview";
  }
  if (sec === "groups") {
    if (f === "fleetSize") row.fleetSize = el.value.trim() === "" ? null : Math.max(1, Math.round(coerceNum(el)));
    else if (f === "name") row.name = el.value.trim();
    else row[f] = el.value;
    return "preview";
  }
  return "none";
}

function handleAction(action, i) {
  switch (action) {
    case "bm-add": state.bmSpecs.push(blank.bm()); break;
    case "bm-del": state.bmSpecs.splice(i, 1); break;
    case "vm-add": state.vmSpecs.push(blank.vm()); break;
    case "vm-del": state.vmSpecs.splice(i, 1); break;
    case "vm-derive": {
      const bm = state.bmSpecs.find((b) => b.name === $("#derive-model").value) ?? state.bmSpecs[0];
      const k = Number($("#derive-k").value) || 1;
      if (!bm) return false;
      const label = { 1: "full", 2: "half", 3: "third", 4: "quarter" }[k] ?? `1of${k}`;
      const gpu = {};
      for (const [m, n] of Object.entries(bm.gpu || {})) gpu[m] = Math.floor(n / k);
      state.vmSpecs.push({
        name: state.vmSpecs.some((s) => s.name === label) ? `${label}-${bm.name}` : label,
        cpu: Math.floor(bm.cpu / k), memGiB: Math.floor(bm.memGiB / k), disk: Math.floor(bm.disk / k), gpu,
      });
      break;
    }
    case "demand-add": state.demands.push(blank.demand()); break;
    case "demand-dup": state.demands.splice(i + 1, 0, { ...state.demands[i] }); break;
    case "demand-del": state.demands.splice(i, 1); break;
    case "group-add": state.groups.push(blank.group()); break;
    case "group-del": state.groups.splice(i, 1); break;
    case "order-up": if (i > 0) [state.buildOrder[i - 1], state.buildOrder[i]] = [state.buildOrder[i], state.buildOrder[i - 1]]; break;
    case "order-down": if (i < state.buildOrder.length - 1) [state.buildOrder[i + 1], state.buildOrder[i]] = [state.buildOrder[i], state.buildOrder[i + 1]]; break;
    default: return false;
  }
  renderForm();
  return true;
}

/* ── request ↔ form ───────────────────────────────────────────────── */

const toRes = (s) => ({
  cpu_cores: s.cpu, memory_mib: Math.round(s.memGiB * GiB), storage_gb: s.disk,
  gpu: Object.fromEntries(Object.entries(s.gpu || {}).filter(([, n]) => n > 0)),
});
const fromRes = (r) => ({
  cpu: r?.cpu_cores ?? 0, memGiB: Math.round(((r?.memory_mib ?? 0) / GiB) * 100) / 100,
  disk: r?.storage_gb ?? 0, gpu: { ...(r?.gpu || {}) },
});

/** The request the server receives. Throws a user-facing Error on
 * structural problems the server would reject anyway (names, references,
 * group membership); arithmetic problems are left to the server so its
 * INPUT_ERROR text is the single source of truth. */
export function buildRequest({ withMeta = false } = {}) {
  const dup = (xs, what) => {
    const seen = new Set();
    for (const x of xs) {
      if (!x) throw new Error(`${what}: every entry needs a name`);
      if (seen.has(x)) throw new Error(`${what}: duplicate name '${x}'`);
      seen.add(x);
    }
  };
  if (!state.bmSpecs.length) throw new Error("Add at least one machine model.");
  if (!state.vmSpecs.length) throw new Error("Add at least one VM spec.");
  if (!state.demands.length) throw new Error("Add at least one demand.");
  dup(state.bmSpecs.map((b) => b.name.trim()), "Machine models");
  dup(state.vmSpecs.map((v) => v.name.trim()), "VM specs");
  dup(state.groups.map((g) => g.name.trim()), "Groups");
  const specNames = new Set(state.vmSpecs.map((v) => v.name.trim()));
  for (const d of state.demands) {
    if (!d.cluster.trim()) throw new Error("Every demand needs a cluster id.");
    if (!specNames.has(d.spec)) throw new Error(`Demand ${d.cluster}/${d.role}: pick a VM spec.`);
  }
  if (state.groups.length) {
    for (const c of clusterNames()) {
      if (!groupOf(c)) throw new Error(`Cluster '${c}' is in no tenancy group — add it to one (or delete every group for a single shared pool).`);
    }
  }
  reconcileOrder();
  const req = {
    bm_specs: state.bmSpecs.map((b) => ({ name: b.name.trim(), capacity: toRes(b) })),
    vm_specs: state.vmSpecs.map((v) => ({ name: v.name.trim(), resources: toRes(v) })),
    demands: state.demands.map((d) => {
      const out = {
        cluster_id: d.cluster.trim(), node_role: d.role.trim() || "worker", ip_type: d.ip,
        vm_spec: d.spec,
      };
      if (d.basis === "vm") out.vm_count = d.n; else out.bm_equivalents = d.n;
      if (d.minDistinct !== null && d.minDistinct !== undefined) out.min_distinct_bms = d.minDistinct;
      if (d.step.trim()) out.step = d.step.trim();
      return out;
    }),
    groups: state.groups.map((g) => {
      const out = { name: g.name.trim(), clusters: [...g.clusters], bm_spec: g.bmSpec };
      if (g.fleetSize !== null && g.fleetSize !== undefined) out.fleet_size = g.fleetSize;
      return out;
    }),
    exclusive_clusters: state.exclusive.filter((c) => clusterNames().includes(c)),
    default_min_distinct_bms: state.policy.defaultMinDistinct ?? 0,
    topology: { ...state.topo },
    build_order: [...state.buildOrder],
    config: {
      max_solve_time_seconds: state.cfg.maxSolve,
      auto_generate_anti_affinity: state.cfg.autoAA,
      w_headroom: state.cfg.wHeadroom,
    },
    max_baremetals: state.cfg.maxBaremetals,
    max_probes: state.cfg.maxProbes,
    deadline_seconds: state.cfg.deadline,
  };
  if (withMeta) {
    const meta = {};
    if (state.name.trim()) meta._name = state.name.trim();
    if (state.description.trim()) meta._description = state.description;
    return { ...meta, ...req };
  }
  return req;
}

/** Inverse of buildRequest. Accepts anything shaped like
 * BmDemandPlanRequest; unknown keys are ignored, missing ones get defaults. */
export function loadIntoForm(req) {
  if (!req || typeof req !== "object" || !Array.isArray(req.demands)) {
    throw new Error("not a PM demand template (no `demands` array)");
  }
  state.name = String(req._name ?? "");
  state.description = String(req._description ?? "");
  state.bmSpecs = (req.bm_specs ?? []).map((b) => ({ name: b.name, ...fromRes(b.capacity) }));
  state.vmSpecs = (req.vm_specs ?? []).map((v) => ({ name: v.name, ...fromRes(v.resources) }));
  state.demands = req.demands.map((d) => ({
    cluster: d.cluster_id ?? "", role: d.node_role ?? "worker", ip: d.ip_type ?? "",
    spec: d.vm_spec ?? "",
    basis: d.vm_count != null && d.bm_equivalents == null ? "vm" : "bm",
    n: d.vm_count != null && d.bm_equivalents == null ? d.vm_count : (d.bm_equivalents ?? 1),
    minDistinct: d.min_distinct_bms ?? null,
    step: d.step ?? "",
  }));
  state.groups = (req.groups ?? []).map((g) => ({
    name: g.name, clusters: [...(g.clusters ?? [])], bmSpec: g.bm_spec, fleetSize: g.fleet_size ?? null,
  }));
  state.exclusive = [...(req.exclusive_clusters ?? [])];
  state.policy.defaultMinDistinct = req.default_min_distinct_bms ?? 0;
  const t = req.topology ?? {};
  for (const [k] of TOPO_DIMS) state.topo[k] = Number(t[k] ?? (k === "racks" ? 6 : k === "ags" ? 3 : 1)) || 1;
  state.buildOrder = [...(req.build_order ?? [])];
  const c = req.config ?? {};
  state.cfg.maxSolve = Number(c.max_solve_time_seconds ?? 10);
  state.cfg.autoAA = c.auto_generate_anti_affinity ?? true;
  state.cfg.wHeadroom = Number(c.w_headroom ?? 0);
  state.cfg.maxBaremetals = Number(req.max_baremetals ?? 200);
  state.cfg.maxProbes = Number(req.max_probes ?? 12);
  state.cfg.deadline = Number(req.deadline_seconds ?? 120);
  reconcileOrder();
  renderForm();
  return true;
}

export function resetForm() {
  state.name = ""; state.description = "";
  state.bmSpecs = []; state.vmSpecs = []; state.demands = []; state.groups = [];
  state.exclusive = []; state.policy.defaultMinDistinct = 6;
  state.topo = { sites: 1, phases: 1, datacenters: 1, rooms: 1, racks: 6, ags: 3 };
  state.buildOrder = [];
  state.cfg = { maxSolve: 10, autoAA: true, wHeadroom: 0, maxBaremetals: 200, maxProbes: 12, deadline: 120 };
  // A useful blank: one 90/720 model, its half and third, and one demand.
  state.bmSpecs.push({ name: "bm-90-720", cpu: 90, memGiB: 720, disk: 6000, gpu: {} });
  state.vmSpecs.push({ name: "full", cpu: 90, memGiB: 720, disk: 6000, gpu: {} });
  state.vmSpecs.push({ name: "half", cpu: 45, memGiB: 360, disk: 3000, gpu: {} });
  state.vmSpecs.push({ name: "third", cpu: 30, memGiB: 240, disk: 2000, gpu: {} });
  state.demands.push({ cluster: "cluster-1", role: "worker", ip: "routable", spec: "half", basis: "bm", n: 6, minDistinct: null, step: "" });
  renderForm();
}

/* ── init ──────────────────────────────────────────────────────────── */

export function initForm(onChange) {
  const root = document.querySelector("#builder");
  renderDatalists(root);

  root.addEventListener("input", (e) => {
    const el = e.target;
    if (el.id === "tpl-name") { state.name = el.value; onChange?.(); return; }
    if (el.id === "cfg-max-solve") { state.cfg.maxSolve = Number(el.value) || 10; onChange?.(); return; }
    if (el.id === "cfg-headroom") { state.cfg.wHeadroom = Math.max(0, Number(el.value) || 0); renderPreviews(); onChange?.(); return; }
    if (el.id === "cfg-auto-aa") { state.cfg.autoAA = el.checked; renderPreviews(); onChange?.(); return; }
    if (el.id === "cfg-max-bm") { state.cfg.maxBaremetals = Number(el.value) || 200; onChange?.(); return; }
    if (el.id === "cfg-max-probes") { state.cfg.maxProbes = Number(el.value) || 12; onChange?.(); return; }
    if (el.id === "cfg-deadline") { state.cfg.deadline = Number(el.value) || 120; onChange?.(); return; }
    if (el.dataset?.policy === "defaultMinDistinct") {
      state.policy.defaultMinDistinct = Math.max(0, Math.round(Number(el.value) || 0));
      renderPreviews(); onChange?.(); return;
    }
    if (el.dataset?.topo) { state.topo[el.dataset.topo] = Math.max(1, Number(el.value) || 1); renderPreviews(); onChange?.(); return; }
    if (el.dataset?.gpick !== undefined) {
      const g = state.groups[Number(el.dataset.gpick)];
      const c = el.dataset.cluster;
      if (g) {
        if (el.checked && !g.clusters.includes(c)) g.clusters.push(c);
        if (!el.checked) g.clusters = g.clusters.filter((x) => x !== c);
      }
      renderForm(); onChange?.(); return;
    }
    if (el.dataset?.excl !== undefined) {
      const c = el.dataset.excl;
      state.exclusive = el.checked
        ? [...new Set([...state.exclusive, c])]
        : state.exclusive.filter((x) => x !== c);
      renderPreviews(); onChange?.(); return;
    }
    if (!el.dataset?.s) return;
    const how = applyFieldEdit(el);
    if (how === "rerender") renderForm();
    else if (how === "rerender-soft") {
      // Cluster / step text is being typed: refresh dependents without
      // rebuilding the input under the cursor.
      $("#group-rows").innerHTML = state.groups.length ? state.groups.map(groupCard).join("") : $("#group-rows").innerHTML;
      $("#policy-card").innerHTML = policyCard();
      $("#order-rows").innerHTML = orderRows();
      refreshClusterDatalist();
      renderPreviews();
    } else if (how === "preview") renderPreviews();
    onChange?.();
  });
  root.addEventListener("change", (e) => {
    const el = e.target;
    if (el.dataset?.s && el.tagName === "SELECT") {
      const how = applyFieldEdit(el);
      if (how === "rerender") renderForm(); else renderPreviews();
      onChange?.();
    }
  });
  root.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    if (handleAction(btn.dataset.act, Number(btn.dataset.i ?? -1))) onChange?.();
  });
}
