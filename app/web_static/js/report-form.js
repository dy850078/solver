/* Structured form for building a CapacityPlanRequest.
 *
 * Catalog-first UX: VM Specs and BM Models are defined once as named
 * catalogs; demand entries, in-stock groups and committed stock then just
 * SELECT from them instead of retyping sizes. The form is the source of
 * truth for Run; the Advanced JSON editor syncs both ways via explicit
 * buttons.
 *
 * Memory is entered in GiB and converted to memory_mib on build. In-stock
 * machines are entered as GROUPS (count × model @ fab/dc/ag/network) and
 * expanded to individual Baremetals on build; loading JSON re-groups them
 * (machine ids are regenerated — they are opaque to the planner).
 *
 * Terminology note: the UI says "BM Model"; the wire contract keeps the
 * existing field names (procurement_types / type_id).
 */

import {
  DEMAND_COLUMNS, DEMAND_HEADER_LINE,
  analyzeDemandCsv, exportDemandCsv, headerish, splitAllowed, validateEntries,
} from "./demand-csv.js";
import {
  STOCK_COLUMNS, STOCK_HEADER_LINE,
  analyzeStockCsv, exportStockCsv, headerishStock, validateStock,
} from "./stock-csv.js";

const $ = (id) => document.getElementById(id);
const GiB = 1024; // MiB per GiB

const state = {
  specs: [],     // {name, cpu, memGiB, disk}
  models: [],    // {model_id, cpu, memGiB, disk, fab, buyable}
  entries: [],   // {..., specIdx: -1 = any spec from the catalog}
  stock: [],     // {count, modelIdx, fab, dc, ag, network}
  caps: [],
  committed: [], // {modelIdx, count, fab, bucket, network}
};

// Config fields the form owns; anything else in loaded JSON (e.g.
// reference_vm_spec, max_solve_time_seconds) is preserved verbatim.
const FORM_CONFIG_FIELDS = new Set([
  "auto_generate_anti_affinity", "max_pods_per_node",
  "procurement_spread_dimension", "w_procurement_balance",
  "fab_topology_dimension", "vm_specs",
]);
let extraConfig = {};

const blank = {
  specs: () => ({ name: "", cpu: 8, memGiB: 16, disk: 100 }),
  models: () => ({ model_id: "", cpu: 64, memGiB: 256, disk: 2000, fab: "", buyable: true }),
  entries: () => ({ cluster_id: "cluster-1", fab: "", period: "", node_role: "worker",
                    cpu: 0, memGiB: 0, disk: 0, pods: 0, specIdx: 0, network: "",
                    allowed: [] }),
  stock: () => ({ count: 1, modelIdx: 0, fab: "", dc: "dc-1", ag: "ag-1", network: "" }),
  caps: () => ({ fab: "", bucket: "ag-1", network: "", max_bm: 1 }),
  committed: () => ({ modelIdx: 0, count: 1, fab: "", bucket: "", network: "" }),
};

/* ── Row templates ── */

const esc = (s) => String(s ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

function num(section, i, field, value, label, { min = 0, step = "any" } = {}) {
  return `<label class="mini"><span class="mini__label">${label}</span>
    <input class="input" type="number" min="${min}" step="${step}" value="${value}"
      data-s="${section}" data-i="${i}" data-f="${field}"></label>`;
}
function txt(section, i, field, value, label, placeholder = "") {
  return `<label class="mini"><span class="mini__label">${label}</span>
    <input class="input" type="text" value="${esc(value)}" placeholder="${esc(placeholder)}"
      data-s="${section}" data-i="${i}" data-f="${field}"></label>`;
}
function delBtn(section, i) {
  return `<button type="button" class="row-del" title="Remove"
    data-del="${section}" data-i="${i}">✕</button>`;
}

function specSelect(section, i, field, value, label, { withAny = false } = {}) {
  const opts = [];
  if (withAny) {
    opts.push(`<option value="-1"${value === -1 ? " selected" : ""}>Any (solver picks)</option>`);
  }
  state.specs.forEach((s, si) => {
    opts.push(`<option value="${si}"${value === si ? " selected" : ""}>${esc(specLabel(s))}</option>`);
  });
  return `<label class="mini"><span class="mini__label">${label}</span>
    <select class="select" data-s="${section}" data-i="${i}" data-f="${field}">${opts.join("")}</select></label>`;
}

function modelSelect(section, i, field, value, label) {
  const opts = state.models.map((m, mi) =>
    `<option value="${mi}"${value === mi ? " selected" : ""}>${esc(modelLabel(m))}</option>`);
  if (!opts.length) opts.push(`<option value="-1">— no models —</option>`);
  return `<label class="mini"><span class="mini__label">${label}</span>
    <select class="select" data-s="${section}" data-i="${i}" data-f="${field}">${opts.join("")}</select></label>`;
}

const specLabel = (s) =>
  `${s.name || "spec"} · ${s.cpu}c/${s.memGiB}g/${s.disk}gb`;
const modelLabel = (m) =>
  `${m.model_id || "model"} · ${m.cpu}c/${m.memGiB}g/${m.disk}gb`;

const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];

function specRow(s, i) {
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${txt("specs", i, "name", s.name, "Name", "e.g. M-8c16g")}
      ${num("specs", i, "cpu", s.cpu, "vCore")}
      ${num("specs", i, "memGiB", s.memGiB, "Mem GiB")}
      ${num("specs", i, "disk", s.disk, "Disk GB")}
      ${delBtn("specs", i)}
    </div>
  </div>`;
}

function modelRow(m, i) {
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${txt("models", i, "model_id", m.model_id, "Model id", "e.g. big-64c")}
      ${num("models", i, "cpu", m.cpu, "vCore")}
      ${num("models", i, "memGiB", m.memGiB, "Mem GiB")}
      ${num("models", i, "disk", m.disk, "Disk GB")}
      ${delBtn("models", i)}
    </div>
    <div class="frow frow--model">
      ${txt("models", i, "fab", m.fab, "Fab", "blank = all fabs")}
      <label class="mini mini--check" title="Can be purchased">
        <input type="checkbox" ${m.buyable ? "checked" : ""}
          data-s="models" data-i="${i}" data-f="buyable">
        <span class="mini__label">Buyable</span>
      </label>
    </div>
  </div>`;
}

/* ── Demand book grid (entry-first: the grid IS state.entries) ── */

const GRID_COLS = [
  { f: "fab",        type: "text",  label: "Fab" },
  { f: "cluster_id", type: "text",  label: "Cluster", cls: "col-cluster" },
  { f: "node_role",  type: "role",  label: "Role" },
  { f: "period",     type: "month", label: "Month" },
  { f: "cpu",        type: "num",   label: "vCore",   cls: "col-num" },
  { f: "memGiB",     type: "num",   label: "Mem GiB", cls: "col-num", step: "any" },
  { f: "disk",       type: "num",   label: "Disk GB", cls: "col-num" },
  { f: "pods",       type: "num",   label: "Pods ≥",  cls: "col-num" },
  { f: "specIdx",    type: "spec",  label: "VM spec", cls: "col-spec" },
  { f: "network",    type: "text",  label: "Network" },
  { f: "allowed",    type: "allowed", label: "Allowed BMs", cls: "col-allowed" },
];
const GRID_RENDER_CAP = 200;
const gridView = { fabs: new Set(), q: "" };

const emptyEntry = () => ({ cluster_id: "", fab: "", period: "", node_role: "worker",
                            cpu: 0, memGiB: 0, disk: 0, pods: 0, specIdx: -1, network: "",
                            allowed: [] });

function gridCell(col, e, i, ghost) {
  const bind = ghost ? `data-ghost="1" data-f="${col.f}"`
                     : `data-s="entries" data-i="${i}" data-f="${col.f}"`;
  const v = e[col.f];
  switch (col.type) {
    case "num":
      return `<input class="dgrid-input" type="number" min="0" step="${col.step || 1}"
        value="${v ?? 0}" ${bind}>`;
    case "month":
      return `<input class="dgrid-input" type="month" value="${esc(v ?? "")}" ${bind}>`;
    case "role":
      return `<select class="dgrid-input" ${bind}>${ROLES.map((r) =>
        `<option value="${r}"${r === v ? " selected" : ""}>${r}</option>`).join("")}</select>`;
    case "spec": {
      const opts = [`<option value="-1"${v === -1 ? " selected" : ""}>Any (solver picks)</option>`,
        ...state.specs.map((s, si) =>
          `<option value="${si}"${v === si ? " selected" : ""}>${esc(specLabel(s))}</option>`)];
      return `<select class="dgrid-input" ${bind}>${opts.join("")}</select>`;
    }
    case "allowed":
      return `<input class="dgrid-input" type="text" placeholder="any"
        title="BM models this cluster may buy/draw; separate with ; — blank = any"
        value="${esc((v || []).join("; "))}" ${bind}>`;
    default:
      return `<input class="dgrid-input" type="text" value="${esc(v ?? "")}" ${bind}>`;
  }
}

const gridActionsHtml = (i) => `
  <button type="button" class="dgrid-act" data-act="dupm" data-i="${i}" title="Duplicate to next month">+1M</button>
  <button type="button" class="dgrid-act" data-act="dup" data-i="${i}" title="Duplicate row">⧉</button>
  <button type="button" class="dgrid-act dgrid-act--del" data-act="del" data-i="${i}" title="Delete row">✕</button>`;

const ghostRowHtml = () => `<tr class="dgrid-row--ghost">
  ${GRID_COLS.map((c) => `<td class="${c.cls || ""}">${gridCell(c, emptyEntry(), -1, true)}</td>`).join("")}
  <td></td><td class="dgrid-status" style="color:var(--text-subtle)">new row — just type</td>
</tr>`;

const entryInView = (e) =>
  (!gridView.fabs.size || gridView.fabs.has(e.fab || "")) &&
  (!gridView.q || (e.cluster_id || "").toLowerCase().includes(gridView.q));

function renderGridFabChips() {
  const bar = $("demand-fab-filter");
  const fabs = [...new Set(state.entries.map((e) => e.fab || ""))];
  if (fabs.length < 2) { bar.innerHTML = ""; gridView.fabs.clear(); return; }
  bar.innerHTML =
    `<button type="button" class="fchip${gridView.fabs.size ? "" : " fchip--on"}" data-fab="*">All fabs</button>` +
    fabs.map((f) => `<button type="button" class="fchip${gridView.fabs.has(f) ? " fchip--on" : ""}"
        data-fab="${esc(f)}">${esc(f || "(single-fab)")}</button>`).join("");
}

function updateDemandCount(rowErrors, bookErrors) {
  const bad = rowErrors.filter((r) => r.length).length;
  const fabs = new Set(state.entries.map((e) => e.fab || "(single)")).size;
  const clusters = new Set(state.entries.map((e) => e.cluster_id).filter(Boolean)).size;
  $("demand-count").textContent =
    `${state.entries.length} entries · ${fabs} fab(s) · ${clusters} cluster(s)`
    + (bad ? ` · ${bad} to fix` : "");
  const box = $("demand-io-msg");
  if (bookErrors.length) {
    box.className = "alert alert--error";
    box.innerHTML = bookErrors.map(esc).join("<br>");
    box.classList.remove("hidden");
  } else if (box.classList.contains("alert--error")) {
    box.classList.add("hidden");
  }
}

function renderDemandGrid() {
  const host = $("demand-grid");
  const { rowErrors, bookErrors } = validateEntries(state.entries, state.models);
  const visible = [];
  state.entries.forEach((e, i) => { if (entryInView(e)) visible.push(i); });
  const shown = visible.slice(0, GRID_RENDER_CAP);

  const ths = GRID_COLS.map((c) => `<th class="${c.cls || ""}">${c.label}</th>`).join("");
  const rows = shown.map((i) => {
    const errs = rowErrors[i];
    return `<tr class="${errs.length ? "dgrid-row--bad" : ""}" data-row="${i}">
      ${GRID_COLS.map((c) => `<td class="${c.cls || ""}">${gridCell(c, state.entries[i], i, false)}</td>`).join("")}
      <td style="white-space:nowrap">${gridActionsHtml(i)}</td>
      <td class="dgrid-status">${errs.length ? esc(errs.join(" · ")) : ""}</td>
    </tr>`;
  }).join("");
  const capNote = visible.length > shown.length
    ? `<tr><td colspan="${GRID_COLS.length + 2}" class="dgrid-cap">Showing ${shown.length} of ${
        visible.length} matching rows — refine the filter to see the rest (every row is still submitted).</td></tr>`
    : "";

  host.innerHTML = `<table class="tbl dgrid">
    <thead><tr>${ths}<th></th><th>Status</th></tr></thead>
    <tbody>${rows}${capNote}${ghostRowHtml()}</tbody>
  </table>`;
  renderGridFabChips();
  updateDemandCount(rowErrors, bookErrors);
}

/* Re-check without rebuilding the DOM (keeps focus while editing). */
function refreshGridValidation() {
  const { rowErrors, bookErrors } = validateEntries(state.entries, state.models);
  $("demand-grid").querySelectorAll("tbody tr[data-row]").forEach((tr) => {
    const errs = rowErrors[+tr.dataset.row] || [];
    tr.classList.toggle("dgrid-row--bad", !!errs.length);
    tr.querySelector(".dgrid-status").textContent = errs.join(" · ");
  });
  updateDemandCount(rowErrors, bookErrors);
}

/* ── In-stock inventory grid (group-first: the grid IS state.stock) ──
 * Mirrors the demand grid so both bulk sections behave identically:
 * ghost row to add, inline per-row validation, dup/del actions, an Excel
 * paste-to-append, fab-filter + free-text search. */

const STOCK_GRID_COLS = [
  { f: "fab",      type: "text",  label: "Fab" },
  { f: "dc",       type: "text",  label: "Datacenter" },
  { f: "ag",       type: "text",  label: "AG" },
  { f: "network",  type: "text",  label: "Network" },
  { f: "modelIdx", type: "model", label: "BM model", cls: "col-spec" },
  { f: "count",    type: "num",   label: "Machines", cls: "col-num", step: 1, min: 1 },
];
const stockView = { fabs: new Set(), q: "" };

const emptyStock = () =>
  ({ count: 1, modelIdx: state.models.length ? 0 : -1, fab: "", dc: "", ag: "", network: "" });

function stockGridCell(col, g, i, ghost) {
  const bind = ghost ? `data-ghost="stock" data-f="${col.f}"`
                     : `data-s="stock" data-i="${i}" data-f="${col.f}"`;
  const v = g[col.f];
  if (col.type === "num")
    return `<input class="dgrid-input" type="number" min="${col.min ?? 0}" step="${col.step || 1}"
      value="${v ?? 0}" ${bind}>`;
  if (col.type === "model") {
    const opts = state.models.map((m, mi) =>
      `<option value="${mi}"${mi === v ? " selected" : ""}>${esc(modelLabel(m))}</option>`);
    if (!opts.length) opts.push(`<option value="-1"${v === -1 ? " selected" : ""}>— no models —</option>`);
    return `<select class="dgrid-input" ${bind}>${opts.join("")}</select>`;
  }
  return `<input class="dgrid-input" type="text" value="${esc(v ?? "")}" ${bind}>`;
}

const stockActionsHtml = (i) => `
  <button type="button" class="dgrid-act" data-act="dup" data-i="${i}" title="Duplicate row">⧉</button>
  <button type="button" class="dgrid-act dgrid-act--del" data-act="del" data-i="${i}" title="Delete row">✕</button>`;

const stockGhostRowHtml = () => `<tr class="dgrid-row--ghost">
  ${STOCK_GRID_COLS.map((c) => `<td class="${c.cls || ""}">${stockGridCell(c, emptyStock(), -1, true)}</td>`).join("")}
  <td></td><td class="dgrid-status" style="color:var(--text-subtle)">new row — just type</td>
</tr>`;

const stockInView = (g) =>
  (!stockView.fabs.size || stockView.fabs.has(g.fab || "")) &&
  (!stockView.q || `${g.fab} ${g.dc} ${g.ag} ${g.network}`.toLowerCase().includes(stockView.q));

function renderStockFabChips() {
  const bar = $("stock-fab-filter");
  const fabs = [...new Set(state.stock.map((g) => g.fab || ""))];
  if (fabs.length < 2) { bar.innerHTML = ""; stockView.fabs.clear(); return; }
  bar.innerHTML =
    `<button type="button" class="fchip${stockView.fabs.size ? "" : " fchip--on"}" data-fab="*">All fabs</button>` +
    fabs.map((f) => `<button type="button" class="fchip${stockView.fabs.has(f) ? " fchip--on" : ""}"
        data-fab="${esc(f)}">${esc(f || "(single-fab)")}</button>`).join("");
}

function updateStockCount(rowErrors) {
  const bad = rowErrors.filter((r) => r.length).length;
  const machines = state.stock.reduce((a, g) => a + (+g.count || 0), 0);
  const fabs = new Set(state.stock.map((g) => g.fab || "(single)")).size;
  $("stock-count").textContent =
    `${state.stock.length} group(s) · ${machines} machine(s) · ${fabs} fab(s)`
    + (bad ? ` · ${bad} to fix` : "");
}

function renderStockGrid() {
  const host = $("stock-grid");
  const { rowErrors } = validateStock(state.stock, state.models);
  const visible = [];
  state.stock.forEach((g, i) => { if (stockInView(g)) visible.push(i); });
  const shown = visible.slice(0, GRID_RENDER_CAP);

  const ths = STOCK_GRID_COLS.map((c) => `<th class="${c.cls || ""}">${c.label}</th>`).join("");
  const rows = shown.map((i) => {
    const errs = rowErrors[i];
    return `<tr class="${errs.length ? "dgrid-row--bad" : ""}" data-row="${i}">
      ${STOCK_GRID_COLS.map((c) => `<td class="${c.cls || ""}">${stockGridCell(c, state.stock[i], i, false)}</td>`).join("")}
      <td style="white-space:nowrap">${stockActionsHtml(i)}</td>
      <td class="dgrid-status">${errs.length ? esc(errs.join(" · ")) : ""}</td>
    </tr>`;
  }).join("");
  const capNote = visible.length > shown.length
    ? `<tr><td colspan="${STOCK_GRID_COLS.length + 2}" class="dgrid-cap">Showing ${shown.length} of ${
        visible.length} matching rows — refine the filter to see the rest (every row is still submitted).</td></tr>`
    : "";

  host.innerHTML = `<table class="tbl dgrid">
    <thead><tr>${ths}<th></th><th>Status</th></tr></thead>
    <tbody>${rows}${capNote}${stockGhostRowHtml()}</tbody>
  </table>`;
  renderStockFabChips();
  updateStockCount(rowErrors);
}

/* Re-check without rebuilding the DOM (keeps focus while editing). */
function refreshStockGridValidation() {
  const { rowErrors } = validateStock(state.stock, state.models);
  $("stock-grid").querySelectorAll("tbody tr[data-row]").forEach((tr) => {
    const errs = rowErrors[+tr.dataset.row] || [];
    tr.classList.toggle("dgrid-row--bad", !!errs.length);
    tr.querySelector(".dgrid-status").textContent = errs.join(" · ");
  });
  updateStockCount(rowErrors);
}

function capRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--cap">
      ${txt("caps", i, "fab", c.fab, "Fab")}
      ${txt("caps", i, "bucket", c.bucket, "Bucket (AG/DC)")}
      ${txt("caps", i, "network", c.network, "Network", "blank = whole bucket")}
      ${num("caps", i, "max_bm", c.max_bm, "Max BM", { step: 1 })}
      ${delBtn("caps", i)}
    </div>
  </div>`;
}

function committedRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--committed">
      ${modelSelect("committed", i, "modelIdx", c.modelIdx, "BM model")}
      ${num("committed", i, "count", c.count, "Count", { min: 1, step: 1 })}
      ${delBtn("committed", i)}
    </div>
    <div class="frow frow--3">
      ${txt("committed", i, "fab", c.fab, "Fab")}
      ${txt("committed", i, "bucket", c.bucket, "Bucket", "blank = floating")}
      ${txt("committed", i, "network", c.network, "Network (BGP)", "blank = any")}
    </div>
  </div>`;
}

const SECTIONS = {
  specs: { el: "spec-rows", row: specRow, deps: ["entries"] },
  models: { el: "model-rows", row: modelRow, deps: ["stock", "committed"] },
  entries: { el: "demand-grid", row: null, deps: [] },   // rendered as a grid
  stock: { el: "stock-grid", row: null, deps: [] },      // rendered as a grid
  caps: { el: "cap-rows", row: capRow, deps: [] },
  committed: { el: "committed-rows", row: committedRow, deps: [] },
};

function renderSection(name) {
  if (name === "entries") { renderDemandGrid(); return; }
  if (name === "stock") { renderStockGrid(); return; }
  const sec = SECTIONS[name];
  const host = $(sec.el);
  host.innerHTML = state[name].map((item, i) => sec.row(item, i)).join("")
    || `<p class="muted form-empty">(none)</p>`;
}

function renderAll() {
  for (const name of Object.keys(SECTIONS)) renderSection(name);
}

/* Catalog row removed → fix up references in dependents. */
function fixupIndexes(field, removed) {
  const fix = (v) => (v === removed ? -1 : v > removed ? v - 1 : v);
  if (field === "specIdx") for (const e of state.entries) e.specIdx = fix(e.specIdx);
  if (field === "modelIdx") {
    for (const g of state.stock) g.modelIdx = Math.max(0, fix(g.modelIdx));
    for (const c of state.committed) c.modelIdx = Math.max(0, fix(c.modelIdx));
  }
}

/* ── Build a CapacityPlanRequest from the form ── */

const toResources = (o) => ({
  cpu_cores: +o.cpu || 0,
  memory_mib: Math.round((+o.memGiB || 0) * GiB),
  storage_gb: +o.disk || 0,
});

export function buildRequest() {
  const demand_book = state.entries.map((e) => {
    const spec = state.specs[e.specIdx];
    return {
      cluster_id: e.cluster_id || "cluster-1",
      node_role: e.node_role,
      period: e.period,
      cpu_cores: +e.cpu || 0,
      memory_mib: Math.round((+e.memGiB || 0) * GiB),
      storage_gb: +e.disk || 0,
      pod_count: +e.pods || 0,
      // A specific spec pins the entry; "Any" (null) falls back to
      // config.vm_specs — the whole catalog — so the solver chooses.
      vm_specs: spec ? [toResources(spec)] : null,
      fab: e.fab || "",
      network: e.network || "",
      allowed_bm_types: e.allowed?.length ? e.allowed : null,
    };
  });

  let bmSeq = 0;
  const in_stock = state.stock.flatMap((g) => {
    const model = state.models[g.modelIdx];
    if (!model) return [];
    return Array.from({ length: +g.count || 0 }, () => {
      bmSeq += 1;
      return {
        id: `bm-${bmSeq}`,
        total_capacity: toResources(model),
        topology: {
          site: g.fab || "", phase: "p1", datacenter: g.dc || "",
          room: "room-1", rack: `rack-${bmSeq}`, ag: g.ag || "",
        },
        network: g.network || "",
      };
    });
  });

  return {
    demand_book,
    in_stock,
    procurement_types: state.models
      .filter((m) => m.buyable && m.model_id)
      .map((m) => ({ type_id: m.model_id, capacity: toResources(m), fab: m.fab || "" })),
    procurement_caps: state.caps
      .filter((c) => c.bucket)
      .map((c) => ({ fab: c.fab || "", bucket: c.bucket,
                     network: c.network || "", max_bm: +c.max_bm || 0 })),
    committed_stock: state.committed
      .filter((c) => state.models[c.modelIdx]?.model_id)
      .map((c) => ({ type_id: state.models[c.modelIdx].model_id,
                     count: +c.count || 0, fab: c.fab || "",
                     bucket: c.bucket || null, network: c.network || "" })),
    config: {
      ...extraConfig,
      auto_generate_anti_affinity: $("cfg-autoaa").checked,
      max_pods_per_node: +$("cfg-maxpods").value || 0,
      procurement_spread_dimension: $("cfg-spread").value,
      w_procurement_balance: +$("cfg-balance").value || 0,
      fab_topology_dimension: "site",
      // The whole spec catalog: the pool "Any" entries draw from.
      vm_specs: state.specs.map(toResources),
    },
  };
}

/* ── Populate the form from a CapacityPlanRequest JSON ── */

const fromMib = (mib) => +((mib || 0) / GiB).toFixed(2);
const resKey = (r) => `${r?.cpu_cores || 0}|${r?.memory_mib || 0}|${r?.storage_gb || 0}`;
const autoName = (r, suffix = "") =>
  `${r.cpu_cores || 0}c-${fromMib(r.memory_mib)}g${suffix}`;

export function loadIntoForm(json) {
  // 1. VM spec catalog: config.vm_specs first, then any per-entry specs.
  state.specs = [];
  const specIdxByKey = new Map();
  const addSpec = (r, name) => {
    const key = resKey(r);
    if (specIdxByKey.has(key)) return specIdxByKey.get(key);
    state.specs.push({
      name: name || autoName(r),
      cpu: r.cpu_cores || 0, memGiB: fromMib(r.memory_mib), disk: r.storage_gb || 0,
    });
    specIdxByKey.set(key, state.specs.length - 1);
    return state.specs.length - 1;
  };
  for (const r of json.config?.vm_specs || []) addSpec(r);

  state.entries = (json.demand_book || []).map((e) => {
    const spec = e.vm_specs?.[0];
    return {
      cluster_id: e.cluster_id || "", fab: e.fab || "", period: e.period || "",
      node_role: e.node_role || "worker",
      cpu: e.cpu_cores || 0,
      memGiB: fromMib(e.memory_mib),
      disk: e.storage_gb || 0,
      pods: e.pod_count || 0,
      specIdx: spec ? addSpec(spec) : -1,
      network: e.network || "",
      allowed: e.allowed_bm_types ?? [],
    };
  });

  // 2. BM model catalog: buyable models from procurement_types; in-stock
  //    capacities that match none become non-buyable legacy models.
  state.models = [];
  const modelIdxByKey = new Map();
  const addModel = (cap, { model_id = "", fab = "", buyable = false } = {}) => {
    const key = resKey(cap);
    if (modelIdxByKey.has(key)) {
      const idx = modelIdxByKey.get(key);
      if (buyable) state.models[idx].buyable = true;
      if (model_id) state.models[idx].model_id = model_id;
      return idx;
    }
    state.models.push({
      model_id: model_id || autoName(cap, "-bm"),
      cpu: cap.cpu_cores || 0, memGiB: fromMib(cap.memory_mib),
      disk: cap.storage_gb || 0, fab, buyable,
    });
    modelIdxByKey.set(key, state.models.length - 1);
    return state.models.length - 1;
  };
  for (const t of json.procurement_types || []) {
    addModel(t.capacity || {}, { model_id: t.type_id, fab: t.fab || "", buyable: true });
  }

  // 3. In-stock: group by (capacity, site, dc, ag, network); ids regenerate.
  const groups = new Map();
  for (const bm of json.in_stock || []) {
    const cap = bm.total_capacity || {};
    const topo = bm.topology || {};
    const key = [resKey(cap), topo.site, topo.datacenter, topo.ag, bm.network].join("|");
    if (!groups.has(key)) {
      groups.set(key, {
        count: 0, modelIdx: addModel(cap),
        fab: topo.site || "", dc: topo.datacenter || "",
        ag: topo.ag || "", network: bm.network || "",
      });
    }
    groups.get(key).count += 1;
  }
  state.stock = [...groups.values()];

  state.caps = (json.procurement_caps || []).map((c) => ({
    fab: c.fab || "", bucket: c.bucket || "",
    network: c.network || "", max_bm: c.max_bm ?? 1,
  }));

  const modelIdxById = new Map(state.models.map((m, i) => [m.model_id, i]));
  state.committed = (json.committed_stock || []).map((c) => ({
    modelIdx: modelIdxById.get(c.type_id) ?? 0,
    count: c.count ?? 1, fab: c.fab || "", bucket: c.bucket || "",
    network: c.network || "",
  }));

  const cfg = json.config || {};
  extraConfig = Object.fromEntries(
    Object.entries(cfg).filter(([k]) => !FORM_CONFIG_FIELDS.has(k)));
  $("cfg-maxpods").value = cfg.max_pods_per_node ?? 0;
  $("cfg-spread").value = cfg.procurement_spread_dimension || "ag";
  $("cfg-balance").value = cfg.w_procurement_balance ?? 0;
  $("cfg-autoaa").checked = cfg.auto_generate_anti_affinity !== false;

  if (state.caps.length || state.committed.length) $("supply-adv").open = true;
  resetGridView();
  resetStockView();
  renderAll();
}

/* ── Wiring ── */

const readVal = (t, f) =>
  t.type === "checkbox" ? t.checked
  : f === "allowed" ? splitAllowed(t.value)
  : f.endsWith("Idx") ? parseInt(t.value, 10)
  : t.type === "number" ? +t.value
  : t.value;

/* Multi-cell paste detection. Tabs/newlines are the Excel signature, but a
 * single CSV line copied from a file has neither — treat ≥3 comma-separated
 * cells as row data too, else it silently falls through as a "single value"
 * and the import appears to do nothing. */
const looksLikeRows = (text) =>
  /[\t\n]/.test(text) || text.split(",").length >= 3;

const nextMonth = (period) => {
  const m = /^(\d{4})-(\d{2})$/.exec(period || "");
  if (!m) return null;
  const d = Number(m[1]) * 12 + (Number(m[2]) - 1) + 1;
  return `${Math.floor(d / 12)}-${String((d % 12) + 1).padStart(2, "0")}`;
};

/* Typing in the ghost row turns it into a real entry (keeping focus) and
 * appends a fresh ghost below. */
function promoteGhost(t) {
  const e = emptyEntry();
  e[t.dataset.f] = readVal(t, t.dataset.f);
  state.entries.push(e);
  const i = state.entries.length - 1;
  const tr = t.closest("tr");
  tr.classList.remove("dgrid-row--ghost");
  tr.dataset.row = i;
  tr.querySelectorAll("[data-ghost]").forEach((inp) => {
    inp.removeAttribute("data-ghost");
    inp.dataset.s = "entries";
    inp.dataset.i = i;
  });
  tr.children[GRID_COLS.length].innerHTML = gridActionsHtml(i);
  tr.querySelector(".dgrid-status").textContent = "";
  tr.insertAdjacentHTML("afterend", ghostRowHtml());
  refreshGridValidation();
}

/* Same, for the in-stock grid's ghost row. */
function promoteStockGhost(t) {
  const g = emptyStock();
  g[t.dataset.f] = readVal(t, t.dataset.f);
  state.stock.push(g);
  const i = state.stock.length - 1;
  const tr = t.closest("tr");
  tr.classList.remove("dgrid-row--ghost");
  tr.dataset.row = i;
  tr.querySelectorAll("[data-ghost]").forEach((inp) => {
    inp.removeAttribute("data-ghost");
    inp.dataset.s = "stock";
    inp.dataset.i = i;
  });
  tr.children[STOCK_GRID_COLS.length].innerHTML = stockActionsHtml(i);
  tr.querySelector(".dgrid-status").textContent = "";
  tr.insertAdjacentHTML("afterend", stockGhostRowHtml());
  refreshStockGridValidation();
}

function resetGridView() {
  gridView.fabs.clear();
  gridView.q = "";
  const search = $("demand-search");
  if (search) search.value = "";
  $("demand-io-msg").classList.add("hidden");   // stale import/paste notes
}

function resetStockView() {
  stockView.fabs.clear();
  stockView.q = "";
  const search = $("stock-search");
  if (search) search.value = "";
  $("stock-io-msg").classList.add("hidden");
}

export function initForm() {
  const sidebar = document.querySelector(".sidebar");
  const demandCard = $("demand-card");
  const stockCard = $("stock-card");

  // Field edits → state (same delegation for the sidebar and the grids).
  const onFieldInput = (ev) => {
    const t = ev.target;
    if (t.dataset.ghost) {
      (t.dataset.ghost === "stock" ? promoteStockGhost : promoteGhost)(t);
      return;
    }
    const { s, i, f } = t.dataset;
    if (!s || !SECTIONS[s]) return;
    state[s][+i][f] = readVal(t, f);
  };
  // Catalog edits refresh the selects that reference them (on blur/commit,
  // so typing a name doesn't lose focus); grid edits re-validate in place.
  const onFieldChange = (ev) => {
    const { s } = ev.target.dataset;
    if (s === "entries") { refreshGridValidation(); return; }
    if (s === "stock") { refreshStockGridValidation(); return; }
    if (!s || !SECTIONS[s]?.deps?.length) return;
    for (const dep of SECTIONS[s].deps) renderSection(dep);
  };
  // Row remove (sidebar cards).
  const onRowDelete = (ev) => {
    const btn = ev.target.closest("[data-del]");
    if (!btn) return;
    const s = btn.dataset.del;
    const i = +btn.dataset.i;
    state[s].splice(i, 1);
    if (s === "specs") fixupIndexes("specIdx", i);
    if (s === "models") fixupIndexes("modelIdx", i);
    renderSection(s);
    for (const dep of SECTIONS[s].deps || []) renderSection(dep);
  };
  for (const root of [sidebar, demandCard, stockCard]) {
    root.addEventListener("input", onFieldInput);
    root.addEventListener("change", onFieldChange);
    root.addEventListener("click", onRowDelete);
  }

  const adders = { "add-spec": "specs", "add-model": "models",
                   "add-cap": "caps", "add-committed": "committed" };
  for (const [btnId, s] of Object.entries(adders)) {
    $(btnId).addEventListener("click", () => {
      state[s].push(blank[s]());
      renderSection(s);
      for (const dep of SECTIONS[s].deps || []) renderSection(dep);
    });
  }

  // Grid row actions: duplicate (to next month), delete.
  $("demand-grid").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".dgrid-act");
    if (!btn) return;
    const i = +btn.dataset.i;
    if (btn.dataset.act === "del") {
      state.entries.splice(i, 1);
    } else {
      const copy = structuredClone(state.entries[i]);
      if (btn.dataset.act === "dupm") copy.period = nextMonth(copy.period) ?? copy.period;
      state.entries.splice(i + 1, 0, copy);
    }
    renderDemandGrid();
  });

  // Grid filters.
  $("demand-fab-filter").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".fchip");
    if (!btn) return;
    if (btn.dataset.fab === "*") gridView.fabs.clear();
    else {
      const f = btn.dataset.fab;
      gridView.fabs.has(f) ? gridView.fabs.delete(f) : gridView.fabs.add(f);
      const all = new Set(state.entries.map((e) => e.fab || ""));
      if (gridView.fabs.size === all.size) gridView.fabs.clear();
    }
    renderDemandGrid();
  });
  $("demand-search").addEventListener("input", (ev) => {
    gridView.q = ev.target.value.trim().toLowerCase();
    renderDemandGrid();
  });

  // Demand book CSV messages + paste-to-append.
  const ioMsg = (kind, html) => {
    const box = $("demand-io-msg");
    box.className = `alert${kind === "error" ? " alert--error" : ""}`;
    box.innerHTML = html;
    box.classList.remove("hidden");
  };

  // Pasting a multi-cell range onto the grid APPENDS rows: with a header
  // it maps by name, without one by the grid's column order. (Import
  // (replace)… is the whole-book swap.)
  $("demand-grid").addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") ?? "";
    if (!looksLikeRows(text)) return;   // single value → into the focused cell
    ev.preventDefault();
    const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
    const full = headerish(text.split("\n", 1)[0], delim)
      ? text
      : DEMAND_COLUMNS.join(delim) + "\n" + text;
    const a = analyzeDemandCsv(full, state.specs, state.models);
    const flat = [...a.globalErrors,
      ...a.rows.flatMap((r) => r.errors.map((m) => `Row ${r.line}: ${m}`))];
    if (flat.length || !a.entries.length) {
      ioMsg("error", `<b>Paste not applied:</b><br>${
        flat.slice(0, 12).map(esc).join("<br>") || "no data rows found"}`);
      return;
    }
    state.entries.push(...a.entries);
    // Message first: a book-level error found by the re-render (e.g. mixed
    // blank/named fabs) must win over the success note, not be masked by it.
    ioMsg("ok", `Appended <b>${a.entries.length}</b> row(s) from paste.`);
    renderDemandGrid();
  });

  $("demand-clear").addEventListener("click", () => {
    const n = state.entries.length;
    state.entries = [];
    resetGridView();
    renderSection("entries");
    if (n) ioMsg("ok", `Cleared <b>${n}</b> entr${n === 1 ? "y" : "ies"} — Import or type in the grid to start over.`);
  });

  $("demand-upload").addEventListener("click", () => $("demand-upload-input").click());
  $("demand-upload-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (file) {
      dlgText = await file.text();
      renderCsvDialog();
      $("csv-dialog").showModal();
    }
    e.target.value = "";
  });
  $("demand-export").addEventListener("click", () => {
    const blob = new Blob([exportDemandCsv(state.entries, state.specs)],
                          { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "demand-book.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // Table-import dialog: paste an Excel range, see it as an editable grid
  // with per-row validation, import only when everything is clean.
  let dlgText = "";

  const renderCsvDialog = () => {
    const host = $("csv-table-host");
    const msgBox = $("csv-dialog-msg");
    const summaryEl = $("csv-dialog-summary");
    const importBtn = $("csv-dialog-import");
    msgBox.classList.add("hidden");

    if (!dlgText.trim()) dlgText = DEMAND_HEADER_LINE;

    // Header-only: show the expected columns and invite a data-only paste.
    const lines = dlgText.trim().split("\n").filter((l) => l.trim());
    if (lines.length === 1) {
      const delim = lines[0].includes("\t") ? "\t" : ",";
      const cols = lines[0].split(delim);
      host.innerHTML = `<div class="table-wrap"><table class="tbl csv-table">
        <thead><tr>${cols.map((h) => `<th>${esc(h.trim())}</th>`).join("")}<th>Status</th></tr></thead>
        <tbody><tr><td colspan="${cols.length + 1}" class="dgrid-cap">
          The header is already set — copy just the data rows from Excel and paste here (Ctrl/⌘+V).
        </td></tr></tbody></table></div>`;
      summaryEl.textContent = "";
      importBtn.disabled = true;
      return;
    }

    const a = analyzeDemandCsv(dlgText, state.specs, state.models);
    const fieldAt = (ci) =>
      Object.entries(a.colOf).find(([, idx]) => idx === ci)?.[0];
    const ths = a.headerRaw.map((h, ci) => fieldAt(ci)
      ? `<th>${esc(h)}</th>`
      : `<th class="csv-col-ignored" title="ignored column">${esc(h)}</th>`
    ).join("");
    const body = a.rows.map((r, ri) => `
      <tr class="${r.errors.length ? "csv-row--bad" : ""}">
        ${a.headerRaw.map((_, ci) =>
          `<td contenteditable="plaintext-only">${esc(r.cells[ci] ?? "")}</td>`).join("")}
        <td class="csv-status">${r.errors.length ? esc(r.errors.join(" · ")) : "✓"}</td>
      </tr>`).join("");
    host.innerHTML = `
      <div class="table-wrap"><table class="tbl csv-table">
        <thead><tr>${ths}<th>Status</th></tr></thead>
        <tbody>${body}</tbody>
      </table></div>`;

    if (a.globalErrors.length || a.warnings.length) {
      msgBox.className = `alert${a.globalErrors.length ? " alert--error" : ""}`;
      msgBox.innerHTML = [...a.globalErrors, ...a.warnings].map(esc).join("<br>");
      msgBox.classList.remove("hidden");
    }
    const badRows = a.rows.filter((r) => r.errors.length).length;
    importBtn.disabled = !a.entries.length;
    summaryEl.textContent = a.summary
      ? `${a.summary.entries} entries · ${a.summary.fabs} fab(s) · ${a.summary.clusters} cluster(s) · ${a.summary.from} → ${a.summary.to}`
      : badRows ? `${badRows} row(s) need fixing` : "";
  };

  const csvFromTable = () => {
    const table = $("csv-table-host").querySelector("table");
    if (!table) return dlgText;
    const clean = (s) => s.replaceAll("\t", " ").replaceAll("\n", " ").trim();
    const header = [...table.querySelectorAll("thead th")].slice(0, -1)
      .map((th) => clean(th.textContent));
    const lines = [...table.querySelectorAll("tbody tr")]
      .filter((tr) => !tr.querySelector(".dgrid-cap"))
      .map((tr) => [...tr.querySelectorAll("td")].slice(0, -1)
        .map((td) => clean(td.textContent)).join("\t"));
    return [header.join("\t"), ...lines].join("\n");
  };

  // Opens seeded with the current book (or just the header when empty), so
  // users can review before replacing — or paste data rows straight in.
  $("demand-table-open").addEventListener("click", () => {
    dlgText = state.entries.length
      ? exportDemandCsv(state.entries, state.specs).trimEnd()
      : DEMAND_HEADER_LINE;
    renderCsvDialog();
    $("csv-dialog").showModal();
  });
  $("csv-dialog-close").addEventListener("click", () => $("csv-dialog").close());
  $("csv-dialog-cancel").addEventListener("click", () => $("csv-dialog").close());

  // Multi-cell paste: WITH a header line it replaces the whole book; data
  // only (no header) replaces the data under the kept header. A single
  // value falls through into the focused cell.
  $("csv-dialog").addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") ?? "";
    if (!looksLikeRows(text)) return;
    ev.preventDefault();
    const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
    if (headerish(text.split("\n", 1)[0], delim)) {
      dlgText = text;
    } else {
      const headerLine = dlgText.trim().split("\n")[0] || DEMAND_HEADER_LINE;
      const hd = headerLine.includes("\t") ? "\t" : ",";
      dlgText = headerLine.split(hd).map((s) => s.trim()).join(delim) + "\n" + text;
    }
    renderCsvDialog();
  });

  // Cell edited → re-serialize the grid and re-validate.
  $("csv-table-host").addEventListener("focusout", () => {
    const next = csvFromTable();
    if (next !== dlgText) {
      dlgText = next;
      renderCsvDialog();
    }
  });

  $("csv-dialog-import").addEventListener("click", () => {
    const a = analyzeDemandCsv(dlgText, state.specs, state.models);
    if (!a.entries.length) return;
    state.entries = a.entries;
    resetGridView();
    renderSection("entries");
    $("csv-dialog").close();
    ioMsg("ok", `Imported <b>${a.summary.entries}</b> entries · ${a.summary.fabs} fab(s) · ${
      a.summary.clusters} cluster(s) · ${a.summary.from} → ${a.summary.to}${
      a.warnings.length ? `<br>${a.warnings.map(esc).join("<br>")}` : ""}`);
  });

  /* ── In-stock grid: mirror of the demand wiring above ── */

  const stockIoMsg = (kind, html) => {
    const box = $("stock-io-msg");
    box.className = `alert${kind === "error" ? " alert--error" : ""}`;
    box.innerHTML = html;
    box.classList.remove("hidden");
  };

  // Grid row actions: duplicate, delete.
  $("stock-grid").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".dgrid-act");
    if (!btn) return;
    const i = +btn.dataset.i;
    if (btn.dataset.act === "del") state.stock.splice(i, 1);
    else state.stock.splice(i + 1, 0, structuredClone(state.stock[i]));
    renderStockGrid();
  });

  // Grid filters.
  $("stock-fab-filter").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".fchip");
    if (!btn) return;
    if (btn.dataset.fab === "*") stockView.fabs.clear();
    else {
      const f = btn.dataset.fab;
      stockView.fabs.has(f) ? stockView.fabs.delete(f) : stockView.fabs.add(f);
      const all = new Set(state.stock.map((g) => g.fab || ""));
      if (stockView.fabs.size === all.size) stockView.fabs.clear();
    }
    renderStockGrid();
  });
  $("stock-search").addEventListener("input", (ev) => {
    stockView.q = ev.target.value.trim().toLowerCase();
    renderStockGrid();
  });

  // Paste an Excel range onto the grid → APPEND groups (header maps by
  // name, headerless by column order).
  $("stock-grid").addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") ?? "";
    if (!looksLikeRows(text)) return;
    ev.preventDefault();
    const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
    const full = headerishStock(text.split("\n", 1)[0], delim)
      ? text
      : STOCK_COLUMNS.join(delim) + "\n" + text;
    const a = analyzeStockCsv(full, state.models);
    const flat = [...a.globalErrors,
      ...a.rows.flatMap((r) => r.errors.map((m) => `Row ${r.line}: ${m}`))];
    if (flat.length || !a.groups.length) {
      stockIoMsg("error", `<b>Paste not applied:</b><br>${
        flat.slice(0, 12).map(esc).join("<br>") || "no data rows found"}`);
      return;
    }
    state.stock.push(...a.groups);
    stockIoMsg("ok", `Appended <b>${a.groups.length}</b> group(s) from paste.`);
    renderStockGrid();
  });

  $("stock-clear").addEventListener("click", () => {
    const n = state.stock.length;
    state.stock = [];
    resetStockView();
    renderSection("stock");
    if (n) stockIoMsg("ok", `Cleared <b>${n}</b> group${n === 1 ? "" : "s"} — Import or type in the grid to start over.`);
  });

  $("stock-upload").addEventListener("click", () => $("stock-upload-input").click());
  $("stock-upload-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (file) {
      stockDlgText = await file.text();
      renderStockDialog();
      $("stock-csv-dialog").showModal();
    }
    e.target.value = "";
  });
  $("stock-export").addEventListener("click", () => {
    const blob = new Blob([exportStockCsv(state.stock, state.models)], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "in-stock.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // Table-import dialog for in-stock.
  let stockDlgText = "";

  const renderStockDialog = () => {
    const host = $("stock-csv-table-host");
    const msgBox = $("stock-csv-dialog-msg");
    const summaryEl = $("stock-csv-dialog-summary");
    const importBtn = $("stock-csv-dialog-import");
    msgBox.classList.add("hidden");

    if (!stockDlgText.trim()) stockDlgText = STOCK_HEADER_LINE;

    const lines = stockDlgText.trim().split("\n").filter((l) => l.trim());
    if (lines.length === 1) {
      const delim = lines[0].includes("\t") ? "\t" : ",";
      const cols = lines[0].split(delim);
      host.innerHTML = `<div class="table-wrap"><table class="tbl csv-table">
        <thead><tr>${cols.map((h) => `<th>${esc(h.trim())}</th>`).join("")}<th>Status</th></tr></thead>
        <tbody><tr><td colspan="${cols.length + 1}" class="dgrid-cap">
          The header is already set — copy just the data rows from Excel and paste here (Ctrl/⌘+V).
        </td></tr></tbody></table></div>`;
      summaryEl.textContent = "";
      importBtn.disabled = true;
      return;
    }

    const a = analyzeStockCsv(stockDlgText, state.models);
    const fieldAt = (ci) => Object.entries(a.colOf).find(([, idx]) => idx === ci)?.[0];
    const ths = a.headerRaw.map((h, ci) => fieldAt(ci)
      ? `<th>${esc(h)}</th>`
      : `<th class="csv-col-ignored" title="ignored column">${esc(h)}</th>`).join("");
    const body = a.rows.map((r) => `
      <tr class="${r.errors.length ? "csv-row--bad" : ""}">
        ${a.headerRaw.map((_, ci) =>
          `<td contenteditable="plaintext-only">${esc(r.cells[ci] ?? "")}</td>`).join("")}
        <td class="csv-status">${r.errors.length ? esc(r.errors.join(" · ")) : "✓"}</td>
      </tr>`).join("");
    host.innerHTML = `<div class="table-wrap"><table class="tbl csv-table">
      <thead><tr>${ths}<th>Status</th></tr></thead><tbody>${body}</tbody></table></div>`;

    if (a.globalErrors.length || a.warnings.length) {
      msgBox.className = `alert${a.globalErrors.length ? " alert--error" : ""}`;
      msgBox.innerHTML = [...a.globalErrors, ...a.warnings].map(esc).join("<br>");
      msgBox.classList.remove("hidden");
    }
    const badRows = a.rows.filter((r) => r.errors.length).length;
    importBtn.disabled = !a.groups.length;
    summaryEl.textContent = a.summary
      ? `${a.summary.groups} group(s) · ${a.summary.machines} machine(s) · ${a.summary.fabs} fab(s) · ${a.summary.buckets} bucket(s)`
      : badRows ? `${badRows} row(s) need fixing` : "";
  };

  const stockCsvFromTable = () => {
    const table = $("stock-csv-table-host").querySelector("table");
    if (!table) return stockDlgText;
    const clean = (s) => s.replaceAll("\t", " ").replaceAll("\n", " ").trim();
    const header = [...table.querySelectorAll("thead th")].slice(0, -1).map((th) => clean(th.textContent));
    const rows = [...table.querySelectorAll("tbody tr")]
      .filter((tr) => !tr.querySelector(".dgrid-cap"))
      .map((tr) => [...tr.querySelectorAll("td")].slice(0, -1).map((td) => clean(td.textContent)).join("\t"));
    return [header.join("\t"), ...rows].join("\n");
  };

  $("stock-table-open").addEventListener("click", () => {
    stockDlgText = state.stock.length
      ? exportStockCsv(state.stock, state.models).trimEnd()
      : STOCK_HEADER_LINE;
    renderStockDialog();
    $("stock-csv-dialog").showModal();
  });
  $("stock-csv-dialog-close").addEventListener("click", () => $("stock-csv-dialog").close());
  $("stock-csv-dialog-cancel").addEventListener("click", () => $("stock-csv-dialog").close());

  $("stock-csv-dialog").addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text") ?? "";
    if (!looksLikeRows(text)) return;
    ev.preventDefault();
    const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
    if (headerishStock(text.split("\n", 1)[0], delim)) {
      stockDlgText = text;
    } else {
      const headerLine = stockDlgText.trim().split("\n")[0] || STOCK_HEADER_LINE;
      const hd = headerLine.includes("\t") ? "\t" : ",";
      stockDlgText = headerLine.split(hd).map((s) => s.trim()).join(delim) + "\n" + text;
    }
    renderStockDialog();
  });

  $("stock-csv-table-host").addEventListener("focusout", () => {
    const next = stockCsvFromTable();
    if (next !== stockDlgText) {
      stockDlgText = next;
      renderStockDialog();
    }
  });

  $("stock-csv-dialog-import").addEventListener("click", () => {
    const a = analyzeStockCsv(stockDlgText, state.models);
    if (!a.groups.length) return;
    state.stock = a.groups;
    resetStockView();
    renderSection("stock");
    $("stock-csv-dialog").close();
    stockIoMsg("ok", `Imported <b>${a.summary.groups}</b> group(s) · ${a.summary.machines} machine(s) · ${
      a.summary.fabs} fab(s) · ${a.summary.buckets} bucket(s)${
      a.warnings.length ? `<br>${a.warnings.map(esc).join("<br>")}` : ""}`);
  });

  // Form ⇄ JSON sync.
  $("form-to-json").addEventListener("click", () => {
    $("json-editor").value = JSON.stringify(buildRequest(), null, 2);
  });
  $("json-to-form").addEventListener("click", () => {
    const errBox = $("json-error");
    errBox.classList.add("hidden");
    try {
      loadIntoForm(JSON.parse($("json-editor").value));
    } catch (e) {
      errBox.textContent = `Invalid JSON: ${e.message}`;
      errBox.classList.remove("hidden");
    }
  });

  // Sensible starting point.
  state.specs = [
    { name: "S-4c8g", cpu: 4, memGiB: 8, disk: 100 },
    { name: "M-8c16g", cpu: 8, memGiB: 16, disk: 200 },
    { name: "L-16c32g", cpu: 16, memGiB: 32, disk: 400 },
  ];
  state.models = [
    { model_id: "std-64c", cpu: 64, memGiB: 256, disk: 2000, fab: "", buyable: true },
  ];
  // Starter row defaults to the current month so a fresh page has no
  // "month is required" error before the user touches anything.
  state.entries = [{ ...blank.entries(), specIdx: 1,
                     period: new Date().toISOString().slice(0, 7) }];
  state.stock = [blank.stock()];
  renderAll();
}
