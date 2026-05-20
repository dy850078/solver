/**
 * AG × Rack anti-affinity matrix.
 *
 * Rows = AG, columns = rack (full path). Cells = VM count.
 * Highlights spreading: if an AG occupies multiple racks, the row shows
 * the spread count (e.g. "spread × 3").
 */

import { colorForAg } from "./colors.js";
import { showTooltip, moveTooltip, hideTooltip, buildTooltipBody } from "./tooltip.js";
import { escapeHtml } from "./util.js";

function rackPath(bm) {
  const t = bm.topology ?? {};
  const segs = [t.site, t.phase, t.datacenter, t.room, t.rack].filter(Boolean);
  return segs.join("/");
}

function rackShortName(bm) {
  const t = bm.topology ?? {};
  return t.rack || t.room || t.datacenter || t.phase || t.site || bm.id;
}

export function buildAgRackMatrix(request, result) {
  const bmById = new Map((request.baremetals ?? []).map((b) => [b.id, b]));

  const rackKeyToBm = new Map();   // first BM that defined this rack key (for naming)
  const racksSet = new Set();
  const agSet = new Set();

  // Discover racks via BMs (so empty racks still appear) and AGs from BMs + assignments
  let usingBmFallback = false;
  for (const bm of request.baremetals ?? []) {
    let key = rackPath(bm);
    if (!key) { key = `bm:${bm.id}`; usingBmFallback = true; }
    racksSet.add(key);
    if (!rackKeyToBm.has(key)) rackKeyToBm.set(key, bm);
    if (bm.topology?.ag) agSet.add(bm.topology.ag);
  }
  for (const a of result.assignments ?? []) {
    if (a.ag) agSet.add(a.ag);
  }

  const cells = new Map();      // "ag|rackKey" -> count
  const agTotals = new Map();
  const rackTotals = new Map();
  for (const a of result.assignments ?? []) {
    const bm = bmById.get(a.baremetal_id);
    if (!bm) continue;
    let key = rackPath(bm);
    if (!key) key = `bm:${bm.id}`;
    const ag = a.ag || "(no ag)";
    agSet.add(ag);
    const k = `${ag}|${key}`;
    cells.set(k, (cells.get(k) ?? 0) + 1);
    agTotals.set(ag, (agTotals.get(ag) ?? 0) + 1);
    rackTotals.set(key, (rackTotals.get(key) ?? 0) + 1);
  }

  const racks = [...racksSet].sort().map((k) => {
    const bm = rackKeyToBm.get(k);
    return {
      key: k,
      name: usingBmFallback ? bm.id : rackShortName(bm),
      path: usingBmFallback ? bm.id : rackPath(bm),
    };
  });

  const ags = [...agSet].sort();

  const dimensionLabel = usingBmFallback ? "BM" : "Rack";

  return {
    racks,
    ags,
    dimensionLabel,
    get: (ag, key) => cells.get(`${ag}|${key}`) ?? 0,
    agTotal: (ag) => agTotals.get(ag) ?? 0,
    rackTotal: (key) => rackTotals.get(key) ?? 0,
    spread: (ag) => racks.reduce((n, r) => n + ((cells.get(`${ag}|${r.key}`) ?? 0) > 0 ? 1 : 0), 0),
    isEmpty: () => racks.length === 0 || ags.length === 0,
  };
}

function cellTooltipNode(ag, rack, count) {
  return buildTooltipBody(
    `${count} VM · ${ag}`,
    [
      ["Rack", rack.path || rack.name],
      ["AG", ag],
    ],
  );
}

export function renderMatrix(container, matrix) {
  const el = typeof container === "string" ? document.querySelector(container) : container;
  if (!el) return;
  if (!matrix || matrix.isEmpty()) {
    el.innerHTML = `<p class="muted" style="margin:0">Not enough data to build a matrix.</p>`;
    return;
  }

  const head = `
    <thead>
      <tr>
        <th class="matrix__corner"><span>AG \\ ${escapeHtml(matrix.dimensionLabel)}</span></th>
        ${matrix.racks.map((r) => `<th class="matrix__col" title="${escapeHtml(r.path)}">${escapeHtml(r.name)}</th>`).join("")}
        <th class="matrix__total" title="VMs in this AG">Σ</th>
      </tr>
    </thead>`;

  const body = matrix.ags.map((ag) => {
    const cells = matrix.racks.map((r) => {
      const v = matrix.get(ag, r.key);
      if (v === 0) return `<td class="matrix__cell"></td>`;
      const alpha = Math.min(0.95, 0.22 + v * 0.18);
      const color = colorForAg(ag);
      return `<td class="matrix__cell matrix__cell--filled"
                style="--cell-color: ${color}; --cell-alpha: ${alpha};"
                data-tip-ag="${escapeHtml(ag)}"
                data-tip-rack="${escapeHtml(r.path || r.name)}"
                data-tip-count="${v}">${v}</td>`;
    }).join("");
    const spread = matrix.spread(ag);
    return `
      <tr>
        <th class="matrix__row">
          <span class="matrix__row-swatch" style="background:${colorForAg(ag)}"></span>
          <span class="matrix__row-name">${escapeHtml(ag)}</span>
          <span class="matrix__row-spread" title="VMs spread across ${spread} ${matrix.dimensionLabel.toLowerCase()}(s)">× ${spread}</span>
        </th>
        ${cells}
        <td class="matrix__total">${matrix.agTotal(ag)}</td>
      </tr>`;
  }).join("");

  const foot = `
    <tfoot>
      <tr>
        <th class="matrix__corner">Σ</th>
        ${matrix.racks.map((r) => `<td class="matrix__total">${matrix.rackTotal(r.key) || ""}</td>`).join("")}
        <td class="matrix__total"></td>
      </tr>
    </tfoot>`;

  el.innerHTML = `<div class="matrix-scroll"><table class="matrix">${head}<tbody>${body}</tbody>${foot}</table></div>`;

  // Cell tooltips
  el.addEventListener("mouseover", (e) => {
    const cell = e.target.closest("[data-tip-ag]");
    if (!cell) return;
    showTooltip(cellTooltipNode(
      cell.dataset.tipAg,
      { path: cell.dataset.tipRack, name: cell.dataset.tipRack },
      parseInt(cell.dataset.tipCount, 10),
    ), e);
  });
  el.addEventListener("mousemove", (e) => {
    if (e.target.closest("[data-tip-ag]")) moveTooltip(e);
  });
  el.addEventListener("mouseout", (e) => {
    if (e.target.closest("[data-tip-ag]")) hideTooltip();
  });
}
