// Compare page — run every scenario of a compare set through
// /api/compare/run (one call per scenario so rows fill in as they finish and
// the user can cancel), then tabulate: grouped by BM model (default) or by
// bundle. The placement detail reuses the Topology page's rack diagram
// (group by / filter / cluster colours) on the cell's PlacementRequest +
// PlacementResult, and lists the exact parameters the cell ran with.

import { listExamples, getExample, compareRun } from "./api.js";
import { escapeHtml } from "./util.js";
import { buildXlsx } from "./xlsx.js";
import { blankSet, loadDraft, saveDraft, clearDraft, normalizeSet, modelsOf, bundleLabel } from "./compare-set.js";
import { initForm, renderForm, readSet, setScenarioStatus, clearScenarioStatus } from "./compare-form.js";
import { GROUP_BY_OPTIONS, buildPanels, collectAgSet, renderRackDiagram, showRackEmpty } from "./rackdiagram.js";
import { rebuildColorScale } from "./colors.js";
import { applyFilter, buildFilterOptions, isFilterActive } from "./filter.js";
import { createMultiSelect } from "./multiselect.js";
import { renderTopologyLegend } from "./summary.js";

const $ = (id) => document.getElementById(id);

const state = {
  set: null,            // the normalized set of the last run
  results: new Map(),   // scenario name → ScenarioResult
  order: [],            // scenario names in run order
  running: false,
  cancelled: false,
  selected: null,
  // Placement detail (Topology-page pipeline on the selected cell)
  detail: {
    req: null, res: null,
    groupBy: "rack",
    showCapacity: (() => { try { return localStorage.getItem("solver-show-capacity") === "1"; } catch { return false; } })(),
    filter: { clusters: new Set(), roles: new Set(), ipTypes: new Set() },
  },
};
let clusterMs = null;
let roleMs = null;
let ipTypeMs = null;

/* ── helpers ─────────────────────────────────────────────────────── */

function showError(msg) {
  const e = $("form-error");
  e.textContent = msg;
  e.classList.remove("hidden");
}
function hideError() { $("form-error").classList.add("hidden"); }

function download(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

const fmt = (v, d = 1) => (v == null ? "—" : Number(v).toFixed(d));
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const chipFor = (status) => ({
  ok: ["good", "ok"], infeasible: ["critical", "infeasible"], error: ["critical", "error"],
  skipped: ["warning", "skipped"], pending: ["neutral", "pending"], running: ["neutral", "running…"],
}[status] || ["neutral", status]);
const chip = (status) => {
  const [kind, label] = chipFor(status);
  return `<span class="chip chip--${kind}">${escapeHtml(label)}</span>`;
};
const ubar = (v) => (v == null ? "—"
  : `<span class="ubar"><span class="ubar__track"><span class="ubar__fill${v > 0.9 ? " ubar__fill--hot" : ""}" style="width:${Math.min(100, Math.round(v * 100))}%"></span></span>${pct(v)}</span>`);

function labelsOf(sc) {
  return { bm_model: modelsOf(sc).join("+"), bundle: sc.bundle, clusters: sc.clusters };
}

function capSummary(c = {}) {
  const gpu = Object.entries(c.gpu || {}).map(([k, v]) => `${k}×${v}`).join(" ");
  return `${c.cpu_cores}c / ${Math.round((c.memory_mib || 0) / 1024)} GiB / ${c.storage_gb} GB${gpu ? " / " + gpu : ""}`;
}

function modelSummary(set, name) {
  const parts = name.split("+").map((n) => set.bm_models[n]).filter(Boolean);
  return parts.map((m) => capSummary(m.capacity)).join(" + ");
}

function bundleSummary(set, name) {
  return bundleLabel(set.bundles[name] || []);
}

/* ── results table ───────────────────────────────────────────────── */

function groupBy() {
  return document.querySelector('input[name="group-by"]:checked')?.value || "bm_model";
}

function renderTable() {
  const set = state.set;
  const table = $("results-table");
  if (!set) return;
  $("results-empty").classList.add("hidden");
  $("results-wrap").classList.remove("hidden");
  const key = groupBy();
  const other = key === "bm_model" ? "bundle" : "bm_model";
  const scenarios = set.scenarios.filter((sc) => sc.enabled !== false);
  const groups = new Map();
  for (const sc of scenarios) {
    const l = labelsOf(sc);
    if (!groups.has(l[key])) groups.set(l[key], []);
    groups.get(l[key]).push({ sc, l });
  }
  const head = `<thead><tr>
    <th>scenario</th><th>${other === "bundle" ? "bundle" : "BM model"}</th><th class="num">clusters</th>
    <th class="num" title="BMs the solver actually placed on — the procurement figure at tightness 1.0 (the generator-provisioned fleet is in the CSV / xlsx export as bm_fleet)">BMs</th>
    <th class="num" title="BMs ÷ clusters (average; a shared BM counts for every cluster on it)">BM / cluster</th>
    <th class="num" title="max = VMs on the fullest BM · avg = VMs ÷ BMs used">VM density</th>
    <th>cpu</th><th>mem</th><th>storage</th><th>status</th><th class="num">time</th>
  </tr></thead>`;
  let body = "<tbody>";
  for (const [g, items] of groups) {
    const summary = key === "bm_model" ? modelSummary(set, g) : bundleSummary(set, g);
    body += `<tr class="cmp-group"><td colspan="11">${escapeHtml(g)}<span class="muted">${escapeHtml(summary)}</span></td></tr>`;
    for (const { sc, l } of items) {
      const r = state.results.get(sc.name);
      const status = r ? r.status : "pending";
      const sel = state.selected === sc.name ? " cmp-row--selected" : "";
      const pend = r ? "" : " cmp-row--pending";
      const ov = sc.overrides && Object.keys(sc.overrides).length ? `<span class="ov-mark" title="${escapeHtml(JSON.stringify(sc.overrides))}">⚙</span>` : "";
      const u = r?.utilization || {};
      body += `<tr class="cmp-row${sel}${pend}" data-name="${escapeHtml(sc.name)}">
        <td>${escapeHtml(sc.name)}${ov}</td>
        <td>${escapeHtml(l[other])}</td>
        <td class="num">${l.clusters}</td>
        <td class="num">${r?.bm_used != null ? `<span class="cmp-big">${r.bm_used}</span>` : (r ? "—" : "…")}</td>
        <td class="num">${fmt(r?.bm_per_cluster_avg, 1)}</td>
        <td class="num">${r?.vm_density_max != null ? `<span class="cmp-sub">max</span> ${r.vm_density_max} <span class="cmp-sub">· avg ${fmt(r.vm_density_avg, 1)}</span>` : "—"}</td>
        <td>${ubar(u.cpu_cores)}</td><td>${ubar(u.memory_mib)}</td><td>${ubar(u.storage_gb)}</td>
        <td>${chip(status)}${r?.error ? ` <span class="cmp-sub" title="${escapeHtml(r.error)}">${escapeHtml(r.error.slice(0, 60))}${r.error.length > 60 ? "…" : ""}</span>` : ""}</td>
        <td class="num">${r?.elapsed_seconds != null ? `${fmt(r.elapsed_seconds, 2)}s` : "—"}</td>
      </tr>`;
    }
  }
  body += "</tbody>";
  table.innerHTML = head + body;
  for (const tr of table.querySelectorAll("tr.cmp-row")) {
    tr.addEventListener("click", () => selectScenario(tr.dataset.name));
  }
  $("csv-btn").disabled = $("xlsx-btn").disabled = state.results.size === 0;
}

function renderStats() {
  const c = $("stats");
  const rs = [...state.results.values()];
  if (!rs.length) { c.classList.add("hidden"); return; }
  const n = (s) => rs.filter((r) => r.status === s).length;
  const elapsed = rs.reduce((a, r) => a + (r.elapsed_seconds || 0), 0);
  const tile = (label, value, cls = "") => `<div class="stat ${cls}"><div class="stat__label">${label}</div><div class="stat__value">${value}</div></div>`;
  c.innerHTML =
    tile("Scenarios", `${rs.length}<span class="unit">/ ${state.order.length}</span>`) +
    tile("OK", n("ok"), n("ok") ? "stat--ok" : "") +
    tile("Infeasible", n("infeasible"), n("infeasible") ? "stat--err" : "") +
    tile("Errors / skipped", n("error") + n("skipped"), n("error") + n("skipped") ? "stat--warn" : "") +
    tile("Solve time", `${elapsed.toFixed(1)}<span class="unit">s</span>`);
  c.classList.remove("hidden");
}

/* ── detail: parameters ──────────────────────────────────────────── */

// The exact knobs this cell ran with (from `resolved`): the bundle, BM models
// and VM specs as compact tables, the scalar knobs as key/value groups. Keys
// the scenario overrode over the set's defaults carry a ⚙ marker.
function renderParams(r, sc) {
  const g = r.resolved;
  const body = $("detail-params-body");
  const hint = $("detail-params-hint");
  if (!g) {
    body.innerHTML = `<span class="muted">No resolved request (the scenario did not run).</span>`;
    hint.textContent = "";
    return;
  }
  const ov = new Set(Object.keys(sc?.overrides || {}));
  const mark = (k) => (ov.has(k) ? `<span class="params__ov" title="overrides the set default">⚙</span>` : "");
  const row = (k, v, key = k) => `<div class="params__row"><span class="params__key">${escapeHtml(k)}${mark(key)}</span><span class="params__val">${escapeHtml(String(v))}</span></div>`;
  const group = (title, inner) => `<div class="params__group"><div class="params__title">${escapeHtml(title)}</div>${inner}</div>`;
  const table = (cols, rows, foot = "") =>
    `<table class="ptable"><thead><tr>${cols.map(([c, num]) => `<th${num ? ' class="num"' : ""}>${escapeHtml(c)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.join("")}</tbody>${foot}</table>`;
  const td = (v, cls = "") => `<td${cls ? ` class="${cls}"` : ""}>${v}</td>`;
  const gib = (mib) => `${Math.round((mib || 0) / 1024)} GiB`;
  const gpuText = (gpu) => Object.entries(gpu || {}).map(([k, v]) => `${k}×${v}`).join(" ");

  // Bundle: one row per node group, total VMs per cluster in the footer.
  const groups = g.node_groups || [];
  const perCluster = groups.reduce((a, ng) => a + (ng.count || 0), 0);
  const bundleRows = groups.map((ng) => {
    const flags = [ng.scope === "shared" ? "shared" : "", ng.exclusive ? "exclusive" : ""].filter(Boolean).join(" ");
    return `<tr>${td(escapeHtml(ng.role))}${td(ng.count, "num")}${td(escapeHtml(ng.ip_type || "—"), ng.ip_type ? "" : "dim")}` +
      `${td(escapeHtml(ng.spec || "default"), ng.spec ? "" : "dim")}${td(ng.max_per_bm ?? "∞", ng.max_per_bm != null ? "num" : "num dim")}` +
      `${td(ng.no_colocate_group ? `<span class="tag-pill">${escapeHtml(ng.no_colocate_group)}</span>` : "", "")}${td(escapeHtml(flags), flags ? "" : "dim")}</tr>`;
  });
  const bundleFoot = `<tfoot><tr>${td(`${groups.length} group${groups.length === 1 ? "" : "s"}`)}${td(perCluster, "num")}<td colspan="5" class="dim">VMs per cluster · × ${g.clusters ?? 1} cluster${(g.clusters ?? 1) === 1 ? "" : "s"} = ${perCluster * (g.clusters ?? 1)}</td></tr></tfoot>`;
  const bundleTable = table([["role"], ["count", true], ["ip_type"], ["spec"], ["max/BM", true], ["tag"], ["flags"]], bundleRows, bundleFoot);

  const modelRows = (g.bm_profiles || []).map((p) => {
    const c = p.capacity || {};
    return `<tr>${td(escapeHtml(p.name))}${td(c.cpu_cores ?? 0, "num")}${td(gib(c.memory_mib), "num")}${td(`${c.storage_gb ?? 0} GB`, "num")}` +
      `${td(escapeHtml(gpuText(c.gpu) || "—"), gpuText(c.gpu) ? "" : "dim")}${td(escapeHtml(p.roles?.length ? p.roles.join(", ") : "all"), p.roles?.length ? "" : "dim")}</tr>`;
  });
  const modelTable = table([["model"], ["cpu", true], ["mem", true], ["storage", true], ["gpu"], ["roles"]], modelRows);

  const specRows = Object.entries(g.vm_specs || {}).map(([n, c]) =>
    `<tr>${td(escapeHtml(n))}${td(c.cpu_cores ?? 0, "num")}${td(gib(c.memory_mib), "num")}${td(`${c.storage_gb ?? 0} GB`, "num")}${td(escapeHtml(gpuText(c.gpu) || "—"), gpuText(c.gpu) ? "" : "dim")}</tr>`);
  const specTable = specRows.length ? table([["spec"], ["cpu", true], ["mem", true], ["storage", true], ["gpu"]], specRows) : "";

  const topo = [];
  for (const k of ["sites", "phases", "datacenters", "rooms"]) if (g[k] && g[k] !== 1) topo.push(row(k, g[k]));
  topo.push(row("racks", g.racks ?? 4), row("ags", g.ags ?? 3));
  const rules = [
    row("anti_affinity", g.anti_affinity ? "on" : "off"),
    row("target_spread", Object.entries(g.target_spread || {}).map(([k, v]) => `${k}:${v}`).join(" ") || "—"),
    row("failover", g.failover ? "master→learner N-1" : "off"),
  ];
  if (g.max_per_bm_by_role && Object.keys(g.max_per_bm_by_role).length) {
    rules.push(row("max_per_bm_by_role", JSON.stringify(g.max_per_bm_by_role)));
  }
  const misc = [row("clusters", g.clusters ?? 1), row("tightness", g.tightness ?? 0.7), row("seed", g.seed ?? "random")];
  const co = Object.entries(g.config_overrides || {});
  const cfg = co.length ? co.map(([k, v]) => row(k, JSON.stringify(v), "config_overrides")) : [row("config_overrides", "—")];

  body.innerHTML =
    `<div class="params__kv">${group("Scenario", misc.join(""))}${group("Topology", topo.join(""))}${group("Rules", rules.join(""))}${group("Solver config", cfg.join(""))}</div>` +
    `<div class="params__tables">${group("Bundle (per cluster)", bundleTable)}` +
    `<div class="params__group">${group("BM model", modelTable)}${specTable ? `<div style="height:8px"></div>${group("VM specs", specTable)}` : ""}</div></div>`;
  hint.textContent = `${(g.bm_profiles || []).map((p) => p.name).join("+")} · ${groups.length} group${groups.length === 1 ? "" : "s"} · ${perCluster} VMs/cluster · ${g.clusters ?? 1} cluster${(g.clusters ?? 1) === 1 ? "" : "s"}` +
    (ov.size ? ` · ${ov.size} override${ov.size === 1 ? "" : "s"}` : "");
}

/* ── detail: rack diagram (Topology pipeline) ────────────────────── */

function mapToOptions(countMap) {
  return [...countMap.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([value, count]) => ({ value, count }));
}

function updateFilterControls() {
  const d = state.detail;
  const bar = $("cmp-filter-bar");
  if (!d.req || !d.res) { bar.classList.add("hidden"); return; }
  bar.classList.remove("hidden");
  const opts = buildFilterOptions(d.req, d.res);
  clusterMs.update({ options: mapToOptions(opts.clusters), selected: d.filter.clusters });
  roleMs.update({ options: mapToOptions(opts.roles), selected: d.filter.roles });
  ipTypeMs.update({ options: mapToOptions(opts.ipTypes), selected: d.filter.ipTypes });
  $("cmp-filter-clear").classList.toggle("hidden", !isFilterActive(d.filter));
}

function clearDetailFilter() {
  const d = state.detail;
  d.filter.clusters.clear(); d.filter.roles.clear(); d.filter.ipTypes.clear();
  updateFilterControls();
  renderDetailViz();
}

function renderDetailViz() {
  const d = state.detail;
  const rackEl = $("cmp-rack-container");
  const legendEl = $("cmp-ag-legend");
  if (!d.req || !d.res) return;
  const filtered = applyFilter(d.res, d.req, d.filter);
  // Colours: the cell's own clusters (unfiltered), rebuilt per selection so
  // the legend always matches the chips on screen.
  const clusterSet = new Set([...buildFilterOptions(d.req, d.res).clusters.keys()].filter((c) => !c.startsWith("(")));
  rebuildColorScale(collectAgSet(d.req, d.res), clusterSet);
  const panels = buildPanels(d.req, filtered, d.groupBy, d.filter);
  renderRackDiagram(rackEl, panels, { showCapacity: d.showCapacity });
  renderTopologyLegend(legendEl);
}

function selectScenario(name) {
  state.selected = name;
  renderTable();
  const r = state.results.get(name);
  const card = $("detail-card");
  if (!r) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const sc = state.set?.scenarios.find((s) => s.name === name);
  const l = r.labels || {};
  $("detail-head").innerHTML = `<b>${escapeHtml(name)}</b> ${chip(r.status)}`;
  $("detail-sub").textContent = [
    `${l.bm_model || ""} · ${l.bundle || ""} · ${l.clusters} cluster${l.clusters === 1 ? "" : "s"}`,
    r.bm_used != null ? `${r.bm_used} BM${r.bm_used === 1 ? "" : "s"}` : null,
    r.vm_density_max != null ? `density max ${r.vm_density_max} / avg ${fmt(r.vm_density_avg, 1)}` : null,
    r.escalation_rounds ? `${r.escalation_rounds} escalation round${r.escalation_rounds === 1 ? "" : "s"}` : null,
    r.solve_time_seconds != null ? `solve ${fmt(r.solve_time_seconds, 3)}s` : null,
  ].filter(Boolean).join(" · ");
  renderParams(r, sc);
  const meta = $("detail-meta");
  meta.innerHTML = r.bm_by_cluster
    ? `Distinct BMs per cluster — ${Object.entries(r.bm_by_cluster).map(([c, n]) => `${escapeHtml(c)}: ${n}`).join(" · ")}`
    : "";

  const d = state.detail;
  d.filter.clusters.clear(); d.filter.roles.clear(); d.filter.ipTypes.clear();
  const rackEl = $("cmp-rack-container");
  if (r.error) {
    d.req = d.res = null;
    updateFilterControls();
    showRackEmpty(rackEl, r.error);
    $("cmp-ag-legend").innerHTML = "";
  } else if (!r.placement_result?.success || !r.placement_request) {
    d.req = d.res = null;
    updateFilterControls();
    showRackEmpty(rackEl, `No placement: the solver reported ${r.solver_status || r.status}.`);
    $("cmp-ag-legend").innerHTML = "";
  } else {
    d.req = r.placement_request;
    d.res = r.placement_result;
    updateFilterControls();
    renderDetailViz();
  }
  $("preset-btn").disabled = !r.resolved;
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ── run loop ────────────────────────────────────────────────────── */

function setRunning(on) {
  state.running = on;
  $("run-btn").disabled = on;
  $("cancel-btn").disabled = !on;
  $("run-bar").classList.toggle("hidden", !on && state.results.size === 0);
}

async function runAll() {
  let set;
  try {
    set = normalizeSet(readSet());
    hideError();
  } catch (err) {
    showError(err.message);
    return;
  }
  const targets = set.scenarios.filter((sc) => sc.enabled !== false).map((sc) => sc.name);
  if (!targets.length) { showError("No scenario is enabled."); return; }
  state.set = set;
  state.results.clear();
  state.order = targets;
  state.selected = null;
  state.cancelled = false;
  clearScenarioStatus();
  $("detail-card").classList.add("hidden");
  setRunning(true);
  renderTable();
  renderStats();
  const t0 = performance.now();
  let i = 0;
  for (const name of targets) {
    if (state.cancelled) break;
    $("run-text").textContent = `${i} / ${targets.length}`;
    $("run-fill").style.width = `${Math.round((i / targets.length) * 100)}%`;
    setScenarioStatus(name, chip("running"));
    try {
      const res = await compareRun({ ...set, only: [name], deadline_seconds: 3600, max_scenarios: 1, include_placement: true });
      const r = res.scenarios[0];
      state.results.set(name, r);
      setScenarioStatus(name, chip(r.status) + (r.bm_used != null ? ` <span>${r.bm_used} BM${r.bm_used === 1 ? "" : "s"}</span>` : ""));
    } catch (err) {
      // A whole-set rejection (422: dangling reference, bad knob) — stop.
      showError(`Run failed on "${name}": ${err.message}`);
      setScenarioStatus(name, chip("error"));
      break;
    }
    i += 1;
    $("run-elapsed").textContent = `${((performance.now() - t0) / 1000).toFixed(1)}s`;
    renderTable();
    renderStats();
  }
  $("run-text").textContent = `${state.results.size} / ${targets.length}${state.cancelled ? " (cancelled)" : ""}`;
  $("run-fill").style.width = `${Math.round((state.results.size / targets.length) * 100)}%`;
  setRunning(false);
}

/* ── export ──────────────────────────────────────────────────────── */

const ROW_COLUMNS = ["name", "bm_model", "bundle", "clusters", "status", "bm_used", "bm_fleet",
  "bm_per_cluster_avg", "vm_total", "vm_density_max", "vm_density_avg", "util_cpu", "util_mem",
  "util_storage", "escalation_rounds", "solve_time_seconds", "elapsed_seconds", "error"];

function summaryRows() {
  return state.order.map((name) => {
    const r = state.results.get(name);
    const sc = state.set.scenarios.find((s) => s.name === name);
    const l = labelsOf(sc);
    const u = r?.utilization || {};
    return {
      name, bm_model: l.bm_model, bundle: l.bundle, clusters: l.clusters,
      status: r?.status ?? "", bm_used: r?.bm_used ?? "", bm_fleet: r?.bm_fleet ?? "",
      bm_per_cluster_avg: r?.bm_per_cluster_avg ?? "", vm_total: r?.vm_total ?? "",
      vm_density_max: r?.vm_density_max ?? "", vm_density_avg: r?.vm_density_avg ?? "",
      util_cpu: u.cpu_cores ?? "", util_mem: u.memory_mib ?? "", util_storage: u.storage_gb ?? "",
      escalation_rounds: r?.escalation_rounds ?? "", solve_time_seconds: r?.solve_time_seconds ?? "",
      elapsed_seconds: r?.elapsed_seconds ?? "", error: r?.error ?? "",
    };
  });
}

function placementRows() {
  const rows = [];
  for (const name of state.order) {
    const r = state.results.get(name);
    for (const p of r?.placements || []) {
      for (const v of p.vms) {
        rows.push([name, p.bm_id, p.ag, v.vm_id, v.role, v.cluster_id,
          p.util.cpu_cores ?? "", p.util.memory_mib ?? "", p.util.storage_gb ?? ""]);
      }
    }
  }
  return rows;
}

function exportCsv() {
  const esc = (v) => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = [ROW_COLUMNS.join(","), ...summaryRows().map((r) => ROW_COLUMNS.map((c) => esc(r[c])).join(","))];
  download(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }), `${state.set.name || "compare"}.csv`);
}

function exportXlsx() {
  const summary = [ROW_COLUMNS, ...summaryRows().map((r) => ROW_COLUMNS.map((c) => r[c]))];
  const placement = [["scenario", "bm_id", "ag", "vm_id", "role", "cluster_id", "util_cpu", "util_mem", "util_storage"], ...placementRows()];
  const blob = buildXlsx([
    { name: "Summary", rows: summary, header: true },
    { name: "Placement", rows: placement, header: true },
  ]);
  download(blob, `${state.set.name || "compare"}.xlsx`);
}

/* ── set load / save ─────────────────────────────────────────────── */

let draftTimer = null;
function scheduleDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    try { saveDraft(normalizeSet(readSet())); } catch { /* half-edited form; keep the last good draft */ }
  }, 400);
}

function loadSet(set, { fromExample = false } = {}) {
  hideError();
  renderForm(set);
  if (!fromExample) saveDraft(normalizeSet(set));
  else scheduleDraft();
}

async function populateExamples() {
  const sel = $("example-select");
  try {
    const items = await listExamples();
    for (const item of items.filter((x) => x.endpoint_hint === "compare")) {
      const opt = document.createElement("option");
      opt.value = item.name;
      opt.textContent = item.name.replace(/^compare\//, "");
      sel.appendChild(opt);
    }
  } catch (err) {
    console.error("Failed to load examples", err);
  }
}

function initDetailControls() {
  const d = state.detail;
  const sel = $("cmp-group-by");
  for (const o of GROUP_BY_OPTIONS) {
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    opt.selected = o.value === d.groupBy;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", (e) => { d.groupBy = e.target.value; renderDetailViz(); });
  const cap = $("cmp-show-capacity");
  cap.checked = d.showCapacity;
  cap.addEventListener("change", (e) => {
    d.showCapacity = e.target.checked;
    try { localStorage.setItem("solver-show-capacity", d.showCapacity ? "1" : "0"); } catch { /* ignore */ }
    renderDetailViz();
  });
  const onFilterChange = (key) => (selected) => {
    d.filter[key] = selected;
    $("cmp-filter-clear").classList.toggle("hidden", !isFilterActive(d.filter));
    renderDetailViz();
  };
  clusterMs = createMultiSelect({ label: "Cluster", onChange: onFilterChange("clusters") });
  roleMs = createMultiSelect({ label: "Role", onChange: onFilterChange("roles") });
  ipTypeMs = createMultiSelect({ label: "IP type", onChange: onFilterChange("ipTypes") });
  $("cmp-filter-cluster").appendChild(clusterMs.element);
  $("cmp-filter-role").appendChild(roleMs.element);
  $("cmp-filter-iptype").appendChild(ipTypeMs.element);
  $("cmp-filter-clear").addEventListener("click", clearDetailFilter);
}

function init() {
  initForm({ onChange: scheduleDraft });
  initDetailControls();
  populateExamples();
  const draft = loadDraft();
  renderForm(draft || blankSet());

  $("example-select").addEventListener("change", async (e) => {
    if (!e.target.value) return;
    try {
      const content = await getExample(e.target.value);
      delete content._description;
      loadSet(content, { fromExample: true });
    } catch (err) {
      showError(`Failed to load example: ${err.message}`);
    }
  });
  $("load-btn").addEventListener("click", () => $("load-input").click());
  $("load-input").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const parsed = JSON.parse(ev.target.result);
        if (!parsed || !parsed.scenarios) throw new Error("not a compare set (no `scenarios`)");
        delete parsed._description;
        loadSet(parsed);
      } catch (err) {
        showError(`Failed to load set: ${err.message}`);
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  });
  $("save-btn").addEventListener("click", () => {
    try {
      const set = normalizeSet(readSet());
      hideError();
      download(new Blob([JSON.stringify(set, null, 2)], { type: "application/json" }),
        `${(set.name || "compare-set").replace(/[^\w.\-]+/g, "-")}.json`);
    } catch (err) {
      showError(err.message);
    }
  });
  $("new-btn").addEventListener("click", () => { clearDraft(); loadSet(blankSet()); });
  $("run-btn").addEventListener("click", runAll);
  $("cancel-btn").addEventListener("click", () => { state.cancelled = true; });
  for (const r of document.querySelectorAll('input[name="group-by"]')) r.addEventListener("change", renderTable);
  $("csv-btn").addEventListener("click", exportCsv);
  $("xlsx-btn").addEventListener("click", exportXlsx);
  $("preset-btn").addEventListener("click", () => {
    const r = state.results.get(state.selected);
    if (!r?.resolved) return;
    download(new Blob([JSON.stringify(r.resolved, null, 2)], { type: "application/json" }),
      `${state.selected.replace(/[^\w.\-]+/g, "-")}.mock.json`);
  });
}

init();
