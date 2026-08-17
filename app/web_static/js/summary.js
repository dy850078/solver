import { escapeHtml } from "./util.js";

function statusKind(status) {
  if (!status) return "neutral";
  if (status === "OPTIMAL" || status === "FEASIBLE") return "ok";
  if (status.startsWith("INFEASIBLE") || status.startsWith("INPUT_ERROR") || status.startsWith("MODEL_INVALID")) return "err";
  return "warn";
}

// ─── Stat cards ────────────────────────────────────────────
export function renderStats(container, result) {
  if (!result) {
    container.classList.add("hidden");
    container.innerHTML = "";
    return;
  }
  container.classList.remove("hidden");

  const placed = (result.assignments ?? []).length;
  const unplaced = (result.unplaced_vms ?? []).length;
  const time = (result.solve_time_seconds ?? 0).toFixed(3);
  const status = result.solver_status || "—";
  const kind = statusKind(status);
  const statusClass = kind === "ok" ? "stat--ok" : kind === "err" ? "stat--err" : "stat--warn";

  const statusShort = status.split(":")[0]; // strip long detail in the headline

  const filtered = result._filterActive;
  const totals = result._filterTotals ?? { placed, unplaced };
  const placedDisplay = filtered
    ? `${placed}<span class="unit"> / ${totals.placed}</span>`
    : String(placed);
  const unplacedDisplay = filtered
    ? `${unplaced}<span class="unit"> / ${totals.unplaced}</span>`
    : String(unplaced);

  // BM utilization (used / provided). Independent of VM filtering — always
  // reflects the whole solve, so it does not change when a filter is active.
  const bmUsed = result.bm_used_count ?? 0;
  const bmTotal = result.bm_total_count ?? 0;
  const bmUsedDisplay = `${bmUsed}<span class="unit"> / ${bmTotal}</span>`;

  container.innerHTML = `
    <div class="stat ${statusClass}">
      <span class="stat__label">Status</span>
      <span class="stat__value">${escapeHtml(statusShort)}</span>
    </div>
    <div class="stat">
      <span class="stat__label">Solve time</span>
      <span class="stat__value">${time}<span class="unit">s</span></span>
    </div>
    <div class="stat">
      <span class="stat__label">VMs placed${filtered ? " (filtered)" : ""}</span>
      <span class="stat__value">${placedDisplay}</span>
    </div>
    <div class="stat ${unplaced > 0 ? "stat--err" : ""}">
      <span class="stat__label">VMs unplaced${filtered ? " (filtered)" : ""}</span>
      <span class="stat__value">${unplacedDisplay}</span>
    </div>
    <div class="stat">
      <span class="stat__label">BMs used</span>
      <span class="stat__value">${bmUsedDisplay}</span>
    </div>
  `;
}

// ─── Tables ────────────────────────────────────────────────
function assignmentsTable(assignments) {
  if (!assignments || assignments.length === 0) {
    return `<p class="muted" style="margin:0">No assignments produced.</p>`;
  }
  const rows = assignments.map((a) => `
    <tr>
      <td class="cell-mono">${escapeHtml(a.vm_id)}</td>
      <td>${escapeHtml(a.vm_hostname)}</td>
      <td class="cell-mono">${escapeHtml(a.baremetal_id)}</td>
      <td>${escapeHtml(a.bm_hostname)}</td>
      <td>${escapeHtml(a.ag)}</td>
    </tr>`).join("");
  return `
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr><th>VM ID</th><th>VM hostname</th><th>BM ID</th><th>BM hostname</th><th>AG</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function splitDecisionsTable(decisions) {
  if (!decisions || decisions.length === 0) return "";
  const rows = decisions.map((d) => {
    const r = d.vm_spec ?? {};
    const spec = `${r.cpu_cores ?? 0} vCPU · ${r.memory_mib ?? 0} MiB · ${r.storage_gb ?? 0} GB · ${r.gpu_count ?? 0} GPU`;
    return `<tr><td>${escapeHtml(d.node_role)}</td><td class="cell-mono">${escapeHtml(spec)}</td><td>${d.count}</td></tr>`;
  }).join("");
  return `
    <h3 class="section-title">Split decisions</h3>
    <div class="table-wrap">
      <table class="table">
        <thead><tr><th>Role</th><th>VM spec</th><th>Count</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function unplacedBlock(unplaced) {
  if (!unplaced || unplaced.length === 0) return "";
  return `
    <div class="unplaced-banner">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="8" cy="8" r="6.5"/>
        <path d="M8 5v3.5M8 11h.01"/>
      </svg>
      <span><strong>${unplaced.length}</strong> unplaced VM${unplaced.length > 1 ? "s" : ""}: ${unplaced.map(escapeHtml).join(", ")}</span>
    </div>`;
}

function diagnosticsBlock(diagnostics) {
  if (!diagnostics || Object.keys(diagnostics).length === 0) return "";
  return `
    <details class="diagnostics">
      <summary>Diagnostics</summary>
      <pre>${escapeHtml(JSON.stringify(diagnostics, null, 2))}</pre>
    </details>`;
}

export function renderResult(container, result) {
  if (!result) {
    container.innerHTML = `<p class="muted" style="margin:0">No result yet. Submit a request to see assignments.</p>`;
    return;
  }
  const filtered = result._filterActive;
  const placed = (result.assignments ?? []).length;
  const totalPlaced = result._filterTotals?.placed ?? placed;
  const assignmentsCount = filtered
    ? `(${placed} of ${totalPlaced} — filtered)`
    : `(${placed})`;

  container.innerHTML = `
    ${unplacedBlock(result.unplaced_vms)}
    <h3 class="section-title">Assignments <span class="muted">${assignmentsCount}</span></h3>
    ${assignmentsTable(result.assignments)}
    ${splitDecisionsTable(result.split_decisions)}
    ${diagnosticsBlock(result.diagnostics)}
  `;
}

export function renderLegend(container, entries) {
  if (!entries || entries.length === 0) {
    container.innerHTML = "";
    return;
  }
  const label = `<span style="color: var(--text-subtle); font-weight: 500; margin-right: 4px;">AG</span>`;
  container.innerHTML = label + entries.map((e) => `
    <span class="legend-item">
      <span class="legend-swatch" style="background:${e.color}"></span>
      ${escapeHtml(e.ag)}
    </span>`).join("");
}

export function renderError(container, message) {
  container.innerHTML = `<div class="alert alert--error">${escapeHtml(message)}</div>`;
}
