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

/* ── Fab × Month table ── */
function renderGrid(report) {
  const rows = report.by_fab_period.map((p, i) => {
    const chip = periodChip(p);
    const note = p.shortfalls?.[0]?.message || "";
    return `
      <tr class="row--click" data-idx="${i}">
        <td class="mono">${esc(p.period)}</td>
        <td>${esc(p.fab || "—")}</td>
        <td>${chipHtml(chip)}</td>
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

  $("grid-content").querySelectorAll("tr.row--click").forEach((tr) => {
    tr.addEventListener("click", () => {
      $("grid-content").querySelectorAll("tr.row--selected")
        .forEach((r) => r.classList.remove("row--selected"));
      tr.classList.add("row--selected");
      renderDetail(report.by_fab_period[Number(tr.dataset.idx)]);
    });
  });
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
  const rows = report.budget_view || [];
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
function renderReport(report) {
  renderStats(report);
  renderGrid(report);
  renderBudget(report);
  $("detail-card").classList.add("hidden");
  // Auto-select the first non-OK month (the interesting one), else the first.
  const rows = $("grid-content").querySelectorAll("tr.row--click");
  if (rows.length) {
    const firstBad = report.by_fab_period.findIndex((p) => !p.success);
    rows[firstBad >= 0 ? firstBad : 0].click();
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
