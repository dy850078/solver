/* Capacity Report page — renders a CapacityReport (POST /v1/capacity/plan).
 *
 * Rendering rules follow the dataviz method: stat tiles for headline numbers,
 * tables with tabular-nums for enumerable rows, single-hue meters for
 * utilization, fixed status palette for state (always dot + text label,
 * never color alone), per-mark hover tooltips.
 */

import { listExamples, getExample, planCapacity } from "./api.js";
import { initForm, buildRequest, loadIntoForm } from "./report-form.js";

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const fmt = (n) => Number(n).toLocaleString("en-US");

/* Cause → status chip class + label. Status colors are reserved for state. */
const CAUSE_CHIP = {
  none:          { cls: "chip--good",     label: "OK" },
  space:         { cls: "chip--warning",  label: "SPACE" },
  anti_affinity: { cls: "chip--serious",  label: "ANTI-AFFINITY" },
  capacity:      { cls: "chip--critical", label: "CAPACITY" },
  input_error:   { cls: "chip--critical", label: "INPUT ERROR" },
  blocked:       { cls: "chip--neutral",  label: "BLOCKED" },
  unknown:       { cls: "chip--neutral",  label: "UNKNOWN" },
};

function periodChip(p) {
  if (p.success) return CAUSE_CHIP.none;
  const cause = p.shortfalls?.[0]?.cause || "unknown";
  return CAUSE_CHIP[cause] || CAUSE_CHIP.unknown;
}

function chipHtml(chip) {
  return `<span class="chip ${chip.cls}">${chip.label}</span>`;
}

/* ── Tooltip (per-mark hover) ── */
const tip = $("report-tooltip");
function bindTooltips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("mousemove", (e) => {
      tip.innerHTML = el.dataset.tip;
      tip.classList.add("show");
      const pad = 14;
      const w = tip.offsetWidth, h = tip.offsetHeight;
      let x = e.clientX + pad, y = e.clientY + pad;
      if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
      if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
      tip.style.left = `${x}px`; tip.style.top = `${y}px`;
    });
    el.addEventListener("mouseleave", () => tip.classList.remove("show"));
  });
}

/* ── Stat tiles ── */
function renderStats(report) {
  const months = report.by_fab_period.length;
  const okMonths = report.by_fab_period.filter((p) => p.success).length;
  const overall = report.success
    ? chipHtml(CAUSE_CHIP.none)
    : chipHtml({ cls: "chip--critical", label: "SHORTFALL" });
  const t = report.totals || {};
  $("stats").innerHTML = `
    <div class="stat">
      <div class="stat__label">Plan status</div>
      <div class="stat__value" style="font-size:16px; padding-top:8px">${overall}</div>
      <div class="stat__sub">${okMonths}/${months} fab-months OK</div>
    </div>
    <div class="stat">
      <div class="stat__label">Node adds</div>
      <div class="stat__value">${fmt(t.node_adds ?? 0)}</div>
      <div class="stat__sub">K8s nodes to create</div>
    </div>
    <div class="stat">
      <div class="stat__label">BMs to buy</div>
      <div class="stat__value">${fmt(t.bm_procurement ?? 0)}</div>
      <div class="stat__sub">procurement (budget)</div>
    </div>
    <div class="stat">
      <div class="stat__label">Committed used</div>
      <div class="stat__value">${fmt(t.committed_bm_used ?? 0)}</div>
      <div class="stat__sub">already-owned machines</div>
    </div>
    <div class="stat">
      <div class="stat__label">Solve time</div>
      <div class="stat__value">${(report.solve_time_seconds ?? 0).toFixed(2)}<span style="font-size:14px;color:var(--text-subtle)"> s</span></div>
      <div class="stat__sub">whole horizon</div>
    </div>`;
  $("stats").classList.remove("hidden");
}

/* ── Fab × Month: matrix + list views with a fab filter ──
 * `view.fabs` empty = all fabs shown. data-idx is always the absolute index
 * into report.by_fab_period, so selection survives view/filter switches. */
let currentReport = null;
let selectedIdx = null;
const view = { mode: "matrix", fabs: new Set() };

const fabLabel = (f) => f || "(single fab)";

function pivotAxes(report) {
  const periods = [...new Set(report.by_fab_period.map((p) => p.period))].sort();
  const fabs = [...new Set(report.by_fab_period.map((p) => p.fab))];
  const shown = view.fabs.size ? fabs.filter((f) => view.fabs.has(f)) : fabs;
  const idx = new Map(report.by_fab_period.map((p, i) => [`${p.fab}|${p.period}`, i]));
  return { periods, fabs, shown, idx };
}

function renderFabFilter(report) {
  const bar = $("fab-filter");
  const { fabs } = pivotAxes(report);
  if (fabs.length < 2) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  bar.innerHTML =
    `<button type="button" class="fchip${view.fabs.size ? "" : " fchip--on"}" data-fab="*">All fabs</button>` +
    fabs.map((f) => `<button type="button" class="fchip${view.fabs.has(f) ? " fchip--on" : ""}"
        data-fab="${esc(f)}">${esc(fabLabel(f))}</button>`).join("");
  bar.classList.remove("hidden");
}

function cellSummary(p) {
  // Failed months: numbers would be what-ifs (excluded from all totals), so
  // phrase them as unmet demand instead of a plan.
  if (!p.success)
    return p.node_adds_total ? `needs +${fmt(p.node_adds_total)} nodes` : "";
  const parts = [];
  if (p.node_adds_total) parts.push(`+${fmt(p.node_adds_total)} nodes`);
  if (p.bm_procurement_total) parts.push(`${fmt(p.bm_procurement_total)} BM`);
  if (p.committed_bm_used) parts.push(`${fmt(p.committed_bm_used)} owned`);
  return parts.join(" · ") || "no growth";
}

function renderMatrix(report) {
  const { periods, shown, idx } = pivotAxes(report);
  const body = shown.map((f) => {
    const tds = periods.map((m) => {
      const i = idx.get(`${f}|${m}`);
      if (i == null) return `<td class="pivot-cell pivot-cell--unplanned">—</td>`;
      const p = report.by_fab_period[i];
      return `<td class="pivot-cell pivot-cell--click" data-idx="${i}">
          ${chipHtml(periodChip(p))}
          <div class="pivot-cell__sub">${esc(cellSummary(p))}</div>
        </td>`;
    }).join("");
    return `<tr><th class="pivot-rowhead">${esc(fabLabel(f))}</th>${tds}</tr>`;
  }).join("");

  // The finance line: bought BMs per month across the shown fabs.
  const totals = periods.map((m) => shown.reduce((acc, f) => {
    const i = idx.get(`${f}|${m}`);
    const p = i == null ? null : report.by_fab_period[i];
    return acc + (p?.success ? p.bm_procurement_total || 0 : 0);
  }, 0));

  $("grid-content").innerHTML = `
    <div class="table-wrap">
      <table class="tbl pivot">
        <thead><tr><th>Fab</th>${periods.map((m) => `<th>${esc(m)}</th>`).join("")}</tr></thead>
        <tbody>${body}</tbody>
        <tfoot><tr><th class="pivot-rowhead">BM buys total</th>
          ${totals.map((n) => `<td class="num">${fmt(n)}</td>`).join("")}</tr></tfoot>
      </table>
    </div>`;
  $("grid-content").querySelectorAll(".pivot-cell--click").forEach((td) =>
    td.addEventListener("click", () => selectIdx(Number(td.dataset.idx))));
}

function renderList(report) {
  const { shown } = pivotAxes(report);
  const shownSet = new Set(shown);
  const rows = report.by_fab_period.map((p, i) => {
    if (!shownSet.has(p.fab)) return "";
    const note = p.shortfalls?.[0]?.message || "";
    return `
      <tr class="row--click" data-idx="${i}">
        <td class="mono">${esc(p.period)}</td>
        <td>${esc(fabLabel(p.fab))}</td>
        <td>${chipHtml(periodChip(p))}</td>
        <td class="num">${fmt(p.node_adds_total)}</td>
        <td class="num">${fmt(p.bm_procurement_total)}</td>
        <td class="num">${fmt(p.committed_bm_used)}</td>
        <td class="muted" style="max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap"
            title="${esc(note)}">${esc(note)}</td>
      </tr>`;
  }).join("");

  $("grid-content").innerHTML = `
    <div class="table-wrap">
      <table class="tbl">
        <thead><tr>
          <th>Month</th><th>Fab</th><th>Status</th>
          <th class="num">Node adds</th><th class="num">BM buys</th><th class="num">Committed</th>
          <th>Note</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  $("grid-content").querySelectorAll("tr.row--click").forEach((tr) =>
    tr.addEventListener("click", () => selectIdx(Number(tr.dataset.idx))));
}

function renderGridView(report) {
  (view.mode === "list" ? renderList : renderMatrix)(report);
  markSelection();
}

function markSelection() {
  const host = $("grid-content");
  host.querySelectorAll(".row--selected, .pivot-cell--selected")
    .forEach((el) => el.classList.remove("row--selected", "pivot-cell--selected"));
  if (selectedIdx == null) return;
  const el = host.querySelector(`[data-idx="${selectedIdx}"]`);
  if (el) el.classList.add(el.tagName === "TD" ? "pivot-cell--selected" : "row--selected");
}

function selectIdx(i) {
  selectedIdx = i;
  markSelection();
  renderDetail(currentReport.by_fab_period[i]);
}

/* ── Metric breakdown pivots (one table per metric) ── */
const METRICS = [
  { key: "node_adds_total", title: "Node adds", sub: "K8s nodes created" },
  { key: "bm_procurement_total", title: "BM buys", sub: "machines to purchase (budget)" },
  { key: "committed_bm_used", title: "Committed used", sub: "already-owned machines consumed" },
];

function metricTable(report, metric) {
  const { periods, shown, idx } = pivotAxes(report);
  // null = unplanned month; NaN = failed month (value would be a what-if).
  const val = (f, m) => {
    const i = idx.get(`${f}|${m}`);
    if (i == null) return null;
    const p = report.by_fab_period[i];
    return p.success ? (p[metric.key] || 0) : NaN;
  };
  const rows = shown.map((f) => {
    let total = 0;
    const tds = periods.map((m) => {
      const v = val(f, m);
      if (v == null) return `<td class="num muted">—</td>`;
      if (Number.isNaN(v)) return `<td class="num muted" title="failed month — excluded from totals">✕</td>`;
      total += v;
      return `<td class="num">${fmt(v)}</td>`;
    }).join("");
    return `<tr><th class="pivot-rowhead">${esc(fabLabel(f))}</th>${tds}
      <td class="num pivot-total">${fmt(total)}</td></tr>`;
  }).join("");
  const colTotals = periods.map((m) => shown.reduce((acc, f) => {
    const v = val(f, m);
    return acc + (v && !Number.isNaN(v) ? v : 0);
  }, 0));
  return `
    <p class="subhead">${metric.title} <span class="muted" style="font-weight:400">· ${metric.sub}</span></p>
    <div class="table-wrap" style="margin-bottom:18px">
      <table class="tbl pivot">
        <thead><tr><th>Fab</th>
          ${periods.map((m) => `<th class="num">${esc(m)}</th>`).join("")}
          <th class="num">Total</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr><th class="pivot-rowhead">Total</th>
          ${colTotals.map((n) => `<td class="num">${fmt(n)}</td>`).join("")}
          <td class="num pivot-total">${fmt(colTotals.reduce((a, b) => a + b, 0))}</td></tr></tfoot>
      </table>
    </div>`;
}

function renderMetrics(report) {
  $("metrics-content").innerHTML = METRICS.map((m) => metricTable(report, m)).join("");
  $("metrics-card").classList.remove("hidden");
}

/* ── Month detail ── */
function resourcesLine(r) {
  if (!r) return "—";
  return `${fmt(r.cpu_cores)} c / ${fmt(Math.round((r.memory_mib ?? 0) / 1024))} GiB / ${fmt(r.storage_gb)} GB`;
}

function renderDetail(p) {
  const chip = periodChip(p);
  $("detail-title").textContent = `${p.fab || "(single fab)"} · ${p.period}`;
  $("detail-status-hint").innerHTML = `${chipHtml(chip)} <span class="muted">${esc(p.solver_status || "")}</span>`;

  const procTags = (p.procurement || []).map((d) =>
    `<span class="tag tag--accent"><b>${esc(d.type_id)}</b> × ${fmt(d.count)}</span>`).join("")
    || `<span class="muted" style="font-size:12.5px">no purchase needed</span>`;
  const ownTags = (p.committed_used || []).map((d) =>
    `<span class="tag"><b>${esc(d.type_id)}</b> × ${fmt(d.count)} <span class="muted">(owned)</span></span>`).join("");

  const splits = (p.split_decisions || []).map((d) => `
    <tr>
      <td>${esc(d.node_role)}</td>
      <td class="mono">${resourcesLine(d.vm_spec)}</td>
      <td class="num">${fmt(d.count)}</td>
    </tr>`).join("");

  const shortfalls = (p.shortfalls || []).map((s) => {
    const c = CAUSE_CHIP[s.cause] || CAUSE_CHIP.unknown;
    const sev = c.cls.replace("chip--", "");
    const meta = [
      s.bucket ? `bucket: ${esc(s.bucket)}` : null,
      s.dimension ? `dimension: ${esc(s.dimension)}` : null,
      s.needed != null ? `needed: ${fmt(s.needed)}` : null,
      s.available != null ? `available: ${fmt(s.available)}` : null,
    ].filter(Boolean).join(" · ");
    return `
      <div class="shortfall shortfall--${sev}">
        ${chipHtml(c)}
        <div style="margin-top:6px">${esc(s.message)}</div>
        ${meta ? `<div class="shortfall__meta">${meta}</div>` : ""}
      </div>`;
  }).join("");

  const gauges = `
    <dl class="kv">
      <dt>Nominal available</dt><dd>${resourcesLine(p.nominal_available)}</dd>
      <dt>Remaining node slots</dt>
      <dd>${p.remaining_node_slots == null ? "— (no reference spec)" : fmt(p.remaining_node_slots) + " × reference VM"}</dd>
      <dt>Stranded capacity</dt>
      <dd>${p.stranded_available == null ? "— (no min-useful spec)" : resourcesLine(p.stranded_available)}</dd>
    </dl>`;

  const cells = (p.cells || []).map((c) => {
    const total = c.in_stock_total?.cpu_cores ?? 0;
    const used = c.in_stock_used?.cpu_cores ?? 0;
    const pct = total > 0 ? Math.round((used / total) * 100) : 0;
    const hot = pct >= 90 ? " meter__fill--hot" : "";
    const tipHtml = esc(`<div class="tooltip__title">${c.bucket}${c.network ? " · " + c.network : ""}</div>` +
      `<div class="tooltip__row"><span>CPU used</span><b>${fmt(used)} / ${fmt(total)} c</b></div>` +
      `<div class="tooltip__row"><span>Available</span><b>${resourcesLine(c.in_stock_available)}</b></div>`);
    return `
      <tr>
        <td class="mono">${esc(c.bucket) || "—"}</td>
        <td class="mono">${esc(c.network) || "—"}</td>
        <td class="num">${fmt(c.node_adds)}</td>
        <td class="num">${fmt(c.bm_bought)}</td>
        <td class="num">${fmt(c.committed_used)}</td>
        <td>
          <div class="meter-cell" data-tip="${tipHtml}">
            <div class="meter"><div class="meter__fill${hot}" style="width:${total > 0 ? Math.max(pct, 1) : 0}%"></div></div>
            <span class="pct">${total > 0 ? pct + "%" : "—"}</span>
          </div>
        </td>
      </tr>`;
  }).join("");

  const balance = Object.entries(p.balance_after || {}).map(([bucket, cpu]) =>
    `<span class="tag"><b>${esc(bucket) || "—"}</b> ${fmt(cpu)} c free</span>`).join("");

  $("detail-content").innerHTML = `
    <div class="detail-grid">
      <div>
        <p class="subhead">Procurement</p>
        <div class="chips-row" style="margin-bottom:16px">${procTags}${ownTags}</div>

        <p class="subhead">Node adds (split decisions)</p>
        ${splits ? `
          <div class="table-wrap">
            <table class="tbl">
              <thead><tr><th>Role</th><th>VM spec</th><th class="num">Count</th></tr></thead>
              <tbody>${splits}</tbody>
            </table>
          </div>` : `<p class="muted" style="font-size:12.5px; margin:0">no nodes added this month</p>`}
      </div>
      <div>
        ${shortfalls ? `<p class="subhead">Shortfalls</p>${shortfalls}` : ""}
        <p class="subhead" style="margin-top:${shortfalls ? "16px" : "0"}">Health gauges</p>
        ${gauges}
        ${balance ? `<p class="subhead" style="margin-top:16px">Post-month available CPU by bucket</p>
          <div class="chips-row">${balance}</div>` : ""}
      </div>
    </div>

    ${cells ? `
      <p class="subhead" style="margin-top:20px">Cells — (bucket, network) drill-down · post-month in-stock</p>
      <div class="table-wrap">
        <table class="tbl">
          <thead><tr>
            <th>Bucket</th><th>Network</th>
            <th class="num">Node adds</th><th class="num">BM bought</th><th class="num">Committed</th>
            <th>CPU utilization</th>
          </tr></thead>
          <tbody>${cells}</tbody>
        </table>
      </div>` : ""}`;

  $("detail-card").classList.remove("hidden");
  bindTooltips($("detail-content"));
  $("detail-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ── Budget view ── */
function renderBudget(report) {
  let rows = report.budget_view || [];
  if (view.fabs.size) rows = rows.filter((r) => view.fabs.has(r.fab));
  if (!rows.length) { $("budget-card").classList.add("hidden"); return; }

  // Single-series strip: bought BMs per month (direct labels, no legend).
  const byMonth = new Map();
  for (const r of rows) byMonth.set(r.period, (byMonth.get(r.period) || 0) + r.bm_count);
  const max = Math.max(...byMonth.values());
  $("budget-strip").innerHTML = [...byMonth.entries()].sort().map(([m, n]) => `
    <div class="barstrip__row" data-tip="${esc(`<div class='tooltip__title'>${m}</div><div class='tooltip__row'><span>BMs to buy</span><b>${n}</b></div>`)}">
      <span class="barstrip__label">${esc(m)}</span>
      <div class="barstrip__track"><div class="barstrip__bar" style="width:${(n / max) * 100}%"></div></div>
      <span class="barstrip__val">${fmt(n)}</span>
    </div>`).join("");

  $("budget-table").innerHTML = `
    <thead><tr>
      <th>Fab</th><th>Bucket</th><th>Network</th><th>Month</th><th class="num">BM count</th>
    </tr></thead>
    <tbody>${rows.map((r) => `
      <tr>
        <td>${esc(r.fab) || "—"}</td>
        <td class="mono">${esc(r.bucket) || "—"}</td>
        <td class="mono">${esc(r.network) || "—"}</td>
        <td class="mono">${esc(r.period)}</td>
        <td class="num">${fmt(r.bm_count)}</td>
      </tr>`).join("")}</tbody>`;

  $("budget-card").classList.remove("hidden");
  bindTooltips($("budget-strip"));
}

/* ── Orchestration ── */
function rerenderViews() {
  if (!currentReport) return;
  renderFabFilter(currentReport);
  renderGridView(currentReport);
  renderMetrics(currentReport);
  renderBudget(currentReport);
}

function renderReport(report) {
  currentReport = report;
  selectedIdx = null;
  view.fabs.clear();
  renderStats(report);
  rerenderViews();
  $("detail-card").classList.add("hidden");
  // Auto-select the first non-OK month (the interesting one), else the first.
  if (report.by_fab_period.length) {
    const firstBad = report.by_fab_period.findIndex((p) => !p.success);
    selectIdx(firstBad >= 0 ? firstBad : 0);
  }
}

async function run() {
  const btn = $("run-btn");
  const errBox = $("json-error");
  errBox.classList.add("hidden");
  // The form is the source of truth; the Advanced editor mirrors it.
  const body = buildRequest();
  $("json-editor").value = JSON.stringify(body, null, 2);
  btn.disabled = true;
  btn.querySelector(".btn__label").textContent = "Planning…";
  try {
    renderReport(await planCapacity(body));
  } catch (e) {
    errBox.textContent = e.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.querySelector(".btn__label").textContent = "Run plan";
  }
}

async function init() {
  initForm();
  $("run-btn").addEventListener("click", run);

  document.querySelectorAll('input[name="grid-view"]').forEach((r) =>
    r.addEventListener("change", () => {
      view.mode = r.value;
      if (currentReport) renderGridView(currentReport);
    }));

  $("fab-filter").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".fchip");
    if (!btn || !currentReport) return;
    if (btn.dataset.fab === "*") view.fabs.clear();
    else {
      const f = btn.dataset.fab;
      view.fabs.has(f) ? view.fabs.delete(f) : view.fabs.add(f);
      // Selecting every fab is the same as no filter.
      const all = new Set(currentReport.by_fab_period.map((p) => p.fab));
      if (view.fabs.size === all.size) view.fabs.clear();
    }
    rerenderViews();
  });

  $("upload-btn").addEventListener("click", () => $("upload-input").click());
  $("upload-input").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (file) {
      const text = await file.text();
      $("json-editor").value = text;
      try { loadIntoForm(JSON.parse(text)); } catch { /* editor keeps raw text */ }
    }
    e.target.value = "";
  });

  try {
    const examples = await listExamples();
    const planExamples = examples.filter((x) => x.endpoint_hint === "capacity-plan");
    const sel = $("example-select");
    for (const ex of planExamples) {
      const opt = document.createElement("option");
      opt.value = ex.name;
      opt.textContent = ex.name;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      const data = await getExample(sel.value);
      $("json-editor").value = JSON.stringify(data, null, 2);
      loadIntoForm(data);
    });
  } catch { /* examples are a convenience; the form still works */ }
}

init();
