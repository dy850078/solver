/* In-stock inventory CSV/TSV import & export (long format).
 *
 * One row = one machine group: `count` machines of a BM Model at a
 * (fab, datacenter, ag, network) location. Mirrors demand-csv.js so the
 * two bulk sections behave identically. This is the interchange format for
 * an Inventory export → the form is a viewing/tweaking/simulation surface
 * (UI positioning decision), so in-stock is ingested, not hand-authored.
 *
 * Contract (mirrors demand):
 *  - columns order-free, case-insensitive; unknown columns warn + ignore
 *  - model & count required; the rest default to blank
 *  - model matched by name against the BM Model catalog
 *  - count is a positive integer
 *  - duplicate (fab, datacenter, ag, network, model) keys are an error
 *  - import is all-or-nothing and REPLACES the whole in-stock list
 *
 * analyzeStockCsv() returns the full per-row picture (for the table
 * editor); exportStockCsv() serializes form groups back to canonical CSV.
 */

/* Canonical column order — the grid's visual order and the positional
 * mapping used for headerless pastes. */
export const STOCK_COLUMNS =
  ["fab", "datacenter", "ag", "network", "model", "count"];
export const STOCK_HEADER_LINE = STOCK_COLUMNS.join(",");

// canonical field -> accepted header spellings
const HEADER_ALIASES = {
  fab: ["fab", "site"],
  dc: ["datacenter", "dc", "data_center"],
  ag: ["ag", "availability_group"],
  network: ["network", "bgp", "net"],
  model: ["model", "bm_model", "model_id", "type", "type_id"],
  count: ["count", "machines", "qty", "quantity"],
};

/* Minimal delimited-text parser: handles quoted fields ("" escapes). */
function parseDelimited(text, delim) {
  const rows = [];
  let field = "", row = [], inQuotes = false;
  const pushField = () => { row.push(field); field = ""; };
  const pushRow = () => { pushField(); rows.push(row); row = []; };
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += ch;
    } else if (ch === '"') inQuotes = true;
    else if (ch === delim) pushField();
    else if (ch === "\n") pushRow();
    else if (ch !== "\r") field += ch;
  }
  if (field !== "" || row.length) pushRow();
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

const posInt = (v) => {
  const s = (v ?? "").trim();
  if (s === "") return null;
  const n = Number(s);
  if (!Number.isInteger(n) || n < 1) return null;
  return n;
};

/**
 * Structured analysis of CSV/TSV text against the BM Model catalog.
 * @returns {
 *   headerRaw: string[], colOf: {field: colIdx},
 *   rows: [{line, cells, errors: string[], group|null}],
 *   globalErrors: string[], warnings: string[], groups, summary
 * }
 * groups is non-empty ONLY when there are no errors anywhere.
 */
export function analyzeStockCsv(text, models) {
  const warnings = [], globalErrors = [];
  const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
  const grid = parseDelimited(text, delim);
  const base = { headerRaw: grid[0] ?? [], colOf: {}, rows: [], globalErrors, warnings, groups: [], summary: null };
  if (grid.length < 2) {
    globalErrors.push("Need a header row and at least one data row.");
    return base;
  }

  const colOf = base.colOf;
  const unknown = [];
  grid[0].forEach((h, idx) => {
    const key = h.trim().toLowerCase();
    const field = Object.keys(HEADER_ALIASES).find((f) => HEADER_ALIASES[f].includes(key));
    if (!field) { if (key) unknown.push(h.trim()); return; }
    if (field in colOf) globalErrors.push(`Duplicate column "${h.trim()}".`);
    colOf[field] = idx;
  });
  if (unknown.length) warnings.push(`Ignored unknown column(s): ${unknown.join(", ")}.`);
  if (!("model" in colOf)) globalErrors.push('Missing required column "model".');
  if (!("count" in colOf)) globalErrors.push('Missing required column "count".');

  base.rows = grid.slice(1).map((cells, i) => ({ line: i + 2, cells, errors: [], group: null }));
  if (globalErrors.length) return base;

  const modelIdxByName = new Map(models.map((m, i) => [m.model_id.trim().toLowerCase(), i]));
  const cell = (r, f) => (f in colOf ? (r[colOf[f]] ?? "").trim() : "");
  const rowOfKey = new Map();

  for (const row of base.rows) {
    const r = row.cells;
    const bad = (msg) => row.errors.push(msg);

    const modelName = cell(r, "model");
    let modelIdx = -1;
    if (!modelName) bad("model is required.");
    else {
      modelIdx = modelIdxByName.get(modelName.toLowerCase()) ?? -2;
      if (modelIdx === -2)
        bad(`unknown model "${modelName}" (catalog: ${models.map((m) => m.model_id).join(", ") || "empty"}).`);
    }

    const count = posInt(cell(r, "count"));
    if (count == null) bad(`count must be a positive integer (got "${cell(r, "count")}").`);

    if (row.errors.length) continue;

    const fab = cell(r, "fab"), dc = cell(r, "dc"), ag = cell(r, "ag"), network = cell(r, "network");
    const key = [fab, dc, ag, network, modelName.toLowerCase()].join("|");
    if (rowOfKey.has(key)) {
      bad(`duplicate (fab, datacenter, ag, network, model) — first seen at row ${rowOfKey.get(key)}.`);
      continue;
    }
    rowOfKey.set(key, row.line);

    row.group = { count, modelIdx, fab, dc, ag, network };
  }

  const good = base.rows.filter((r) => r.group);
  const anyRowErrors = base.rows.some((r) => r.errors.length);
  if (!anyRowErrors && !globalErrors.length && good.length) {
    base.groups = good.map((r) => r.group);
    base.summary = {
      groups: base.groups.length,
      machines: base.groups.reduce((a, g) => a + g.count, 0),
      fabs: new Set(base.groups.map((g) => g.fab || "(single fab)")).size,
      buckets: new Set(base.groups.map((g) => `${g.fab}|${g.dc}|${g.ag}|${g.network}`)).size,
    };
  }
  return base;
}

/** Does this first pasted line look like a header row (vs a data row)?
 * ≥2 cells matching known column names = header. */
export function headerishStock(line, delim) {
  const hits = line.split(delim).filter((c) => {
    const key = c.trim().toLowerCase();
    return Object.values(HEADER_ALIASES).some((names) => names.includes(key));
  }).length;
  return hits >= 2;
}

/**
 * Book-level validation of form-shaped stock groups (the grid's source of
 * truth). model is a constrained select; this covers a missing model,
 * non-positive count, and duplicate location+model keys.
 * @returns {rowErrors: string[][], bookErrors: string[]}
 */
export function validateStock(stock, models) {
  const rowErrors = stock.map(() => []);
  const firstOfKey = new Map();
  stock.forEach((g, i) => {
    if (!(models[g.modelIdx]?.model_id || "").trim()) rowErrors[i].push("model is required");
    if (!(Number.isInteger(+g.count) && +g.count >= 1)) rowErrors[i].push("count must be a positive integer");
    if (rowErrors[i].length) return;
    const key = [g.fab || "", g.dc || "", g.ag || "", g.network || "", g.modelIdx].join("|");
    if (firstOfKey.has(key))
      rowErrors[i].push(`duplicate of row ${firstOfKey.get(key) + 1} (same fab, dc, ag, network, model)`);
    else firstOfKey.set(key, i);
  });
  return { rowErrors, bookErrors: [] };
}

const csvField = (v) => {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

/** Export form-shaped stock groups back to canonical CSV. */
export function exportStockCsv(stock, models) {
  const header = STOCK_HEADER_LINE;
  const lines = stock.map((g) => [
    g.fab, g.dc, g.ag, g.network,
    models[g.modelIdx]?.model_id ?? "", g.count || 0,
  ].map(csvField).join(","));
  return [header, ...lines].join("\n") + "\n";
}
