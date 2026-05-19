function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function statusClass(status) {
  if (!status) return "";
  if (status === "OPTIMAL" || status === "FEASIBLE") return "ok";
  if (status === "INFEASIBLE") return "err";
  return "warn";
}

export function renderStatus(statusBar, result) {
  if (!result) {
    statusBar.textContent = "";
    statusBar.className = "status";
    return;
  }
  const parts = [
    `${result.success ? "✓" : "✗"} ${result.solver_status || "?"}`,
    `${(result.solve_time_seconds ?? 0).toFixed(3)}s`,
    `${(result.assignments ?? []).length} placed`,
  ];
  if ((result.unplaced_vms ?? []).length > 0) {
    parts.push(`${result.unplaced_vms.length} unplaced`);
  }
  statusBar.textContent = parts.join("  •  ");
  statusBar.className = `status ${statusClass(result.solver_status)}`;
}

function assignmentsTable(assignments) {
  if (!assignments || assignments.length === 0) {
    return `<p class="muted">No assignments.</p>`;
  }
  const rows = assignments.map((a) => `
    <tr>
      <td>${escapeHtml(a.vm_id)}</td>
      <td>${escapeHtml(a.vm_hostname)}</td>
      <td>${escapeHtml(a.baremetal_id)}</td>
      <td>${escapeHtml(a.bm_hostname)}</td>
      <td>${escapeHtml(a.ag)}</td>
    </tr>`).join("");
  return `
    <table class="result-table">
      <thead>
        <tr><th>VM ID</th><th>VM hostname</th><th>BM ID</th><th>BM hostname</th><th>AG</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function splitDecisionsTable(decisions) {
  if (!decisions || decisions.length === 0) return "";
  const rows = decisions.map((d) => {
    const r = d.vm_spec ?? {};
    const spec = `cpu=${r.cpu_cores ?? 0}, mem=${r.memory_mib ?? 0}MiB, storage=${r.storage_gb ?? 0}GB, gpu=${r.gpu_count ?? 0}`;
    return `<tr><td>${escapeHtml(d.node_role)}</td><td>${escapeHtml(spec)}</td><td>${d.count}</td></tr>`;
  }).join("");
  return `
    <div class="section-subtitle">Split decisions</div>
    <table class="result-table">
      <thead><tr><th>Role</th><th>VM spec</th><th>Count</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function unplacedBlock(unplaced) {
  if (!unplaced || unplaced.length === 0) return "";
  return `<div class="unplaced">Unplaced VMs (${unplaced.length}): ${unplaced.map(escapeHtml).join(", ")}</div>`;
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
    container.innerHTML = `<p class="muted">Run the solver to see assignments.</p>`;
    return;
  }
  container.innerHTML = `
    <div class="section-subtitle">Assignments (${(result.assignments ?? []).length})</div>
    ${assignmentsTable(result.assignments)}
    ${splitDecisionsTable(result.split_decisions)}
    ${unplacedBlock(result.unplaced_vms)}
    ${diagnosticsBlock(result.diagnostics)}
  `;
}

export function renderLegend(container, entries) {
  if (!entries || entries.length === 0) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = entries.map((e) => `
    <span class="legend-item">
      <span class="legend-swatch" style="background:${e.color}"></span>
      ${escapeHtml(e.ag)}
    </span>`).join("");
}
