// Compare page — run every scenario of a compare set through
// /api/compare/run (one call per scenario so rows fill in as they finish and
// the user can cancel), then tabulate: grouped by BM model (default) or by
// bundle, with a per-BM placement detail and CSV / xlsx export.

import { listExamples, getExample, compareRun } from "./api.js";
import { escapeHtml } from "./util.js";
import { buildXlsx } from "./xlsx.js";
import { blankSet, loadDraft, saveDraft, clearDraft, normalizeSet, modelsOf, bundleLabel } from "./compare-set.js";
import { initForm, renderForm, readSet, setScenarioStatus, clearScenarioStatus } from "./compare-form.js";

const $ = (id) => document.getElementById(id);

const state = {
  set: null,            // the normalized set of the last run
  results: new Map(),   // scenario name → ScenarioResult
  order: [],            // scenario names in run order
  running: false,
  cancelled: false,
  selected: null,
};

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

function modelSummary(set, name) {
  const parts = name.split("+").map((n) => set.bm_models[n]).filter(Boolean);
  if (!parts.length) return "";
  return parts.map((m) => {
    const c = m.capacity || {};
    const gpu = Object.entries(c.gpu || {}).map(([k, v]) => `${k}×${v}`).join(" ");
    return `${c.cpu_cores}c / ${Math.round((c.memory_mib || 0) / 1024)} GiB / ${c.storage_gb} GB${gpu ? " / " + gpu : ""}`;
  }).join(" + ");
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
    <th class="num" title="used ÷ clusters">BM / cluster</th>
    <th class="num" title="VMs on the fullest BM (avg over used BMs below)">VM density</th>
    <th>cpu</th><th>mem</th><th>storage</th><th>status</th><th class="num">time</th>
  </tr></thead>`;
  let body = "<tbody>";
  for (const [g, items] of groups) {
    const summary = key === "bm_model" ? modelSummary(set, g) : bundleSummary(set, g);
    body += `<tr class="cmp-group"><td colspan="11">${escapeHtml(g)}<span class="muted">${escapeHtml(summary)}</span></td></tr>`;
    for (const { sc, l } of items) {
      const r = state.results.get(sc.name);
      const status = r ? r.status : (state.running && state.order.includes(sc.name) ? "pending" : "pending");
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
        <td class="num">${r?.vm_density_max != null ? `${r.vm_density_max} <span class="cmp-sub">avg ${fmt(r.vm_density_avg, 1)}</span>` : "—"}</td>
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
  const done = [...state.results.values()];
  $("csv-btn").disabled = $("xlsx-btn").disabled = done.length === 0;
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

/* ── detail ──────────────────────────────────────────────────────── */

function selectScenario(name) {
  state.selected = name;
  renderTable();
  const r = state.results.get(name);
  const card = $("detail-card");
  if (!r) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const l = r.labels || {};
  $("detail-head").innerHTML = `
    <b>${escapeHtml(name)}</b> ${chip(r.status)}
    <span class="detail-meta">${escapeHtml(l.bm_model || "")} · ${escapeHtml(l.bundle || "")} · ${l.clusters} cluster${l.clusters === 1 ? "" : "s"}
    ${r.bm_used != null ? ` · ${r.bm_used} BM${r.bm_used === 1 ? "" : "s"} · density max ${r.vm_density_max} / avg ${fmt(r.vm_density_avg, 1)}` : ""}
    ${r.escalation_rounds ? ` · ${r.escalation_rounds} escalation round${r.escalation_rounds === 1 ? "" : "s"}` : ""}
    ${r.solve_time_seconds != null ? ` · solve ${fmt(r.solve_time_seconds, 3)}s` : ""}</span>`;
  const body = $("detail-body");
  if (r.error) {
    body.innerHTML = `<div class="alert alert--error">${escapeHtml(r.error)}</div>`;
  } else if (!r.placements) {
    body.innerHTML = `<div class="alert alert--warn">No placement: the solver reported ${escapeHtml(r.solver_status || r.status)}.</div>`;
  } else {
    const bmByCluster = r.bm_by_cluster ? Object.entries(r.bm_by_cluster).map(([c, n]) => `${escapeHtml(c)}: ${n}`).join(" · ") : "";
    body.innerHTML = (bmByCluster ? `<div class="detail-meta" style="margin-bottom:10px">Distinct BMs per cluster — ${bmByCluster}</div>` : "") +
      `<div class="bm-list">` + r.placements.map((p) => `
      <div class="bm-card">
        <div class="bm-card__head"><span class="bm-card__id">${escapeHtml(p.bm_id)}</span><span class="bm-card__ag">${escapeHtml(p.ag || "")} · ${p.vms.length} VM${p.vms.length === 1 ? "" : "s"}</span></div>
        <div class="bm-card__vms">${p.vms.map((v) => `<span class="vm-pill" title="${escapeHtml(v.vm_id)}">${escapeHtml(v.role)}<span class="vm-pill__cluster">${escapeHtml(v.cluster_id)}</span></span>`).join("")}</div>
        <div class="bm-card__util">${[["cpu", "cpu_cores"], ["mem", "memory_mib"], ["storage", "storage_gb"]].map(([lab, k]) =>
          p.util[k] == null ? "" : `<span class="ubar"><span>${lab}</span><span class="ubar__track"><span class="ubar__fill${p.util[k] > 0.9 ? " ubar__fill--hot" : ""}" style="width:${Math.min(100, Math.round(p.util[k] * 100))}%"></span></span><span>${pct(p.util[k])}</span></span>`).join("")}
          ${Object.entries(p.util).filter(([k]) => k.startsWith("gpu:")).map(([k, v]) => `<span class="ubar"><span>${escapeHtml(k.slice(4))}</span><span class="ubar__track"><span class="ubar__fill" style="width:${Math.min(100, Math.round(v * 100))}%"></span></span><span>${pct(v)}</span></span>`).join("")}
        </div>
      </div>`).join("") + `</div>`;
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
      const res = await compareRun({ ...set, only: [name], deadline_seconds: 3600, max_scenarios: 1 });
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

function init() {
  initForm({ onChange: scheduleDraft });
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
