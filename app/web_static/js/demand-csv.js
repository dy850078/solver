/* Demand-book CSV/TSV import & export (long format).
 *
 * One row = one (fab, cluster, role, month) demand entry, mapping 1:1 to
 * DemandEntry. This doubles as the interchange format with the future
 * Go-side demand book (UI positioning decision: the form is a viewing/
 * tweaking/simulation surface; the canonical book lives upstream).
 *
 * Contract (agreed):
 *  - columns order-free, case-insensitive; unknown columns warn + ignore
 *  - cluster & period required; the rest default (role=worker, numbers=0)
 *  - memory in GiB (decimal ok) via memory_gib, or memory_mib — not both
 *  - spec matched by name against the VM Spec catalog; blank = Any
 *  - duplicate (fab, cluster, role, period) keys are an error
 *  - import is all-or-nothing and REPLACES the whole demand list
 */

const ROLES = new Set(["worker", "master", "learner", "infra", "l4lb-storage", "bastion"]);

// canonical field -> accepted header spellings
const HEADER_ALIASES = {
  fab: ["fab", "site"],
  cluster: ["cluster", "cluster_id"],
  role: ["role", "node_role"],
  period: ["period", "month"],
  cpu: ["cpu_cores", "cpu", "vcore", "vcores"],
  memGiB: ["memory_gib", "mem_gib", "memory", "mem"],
  memMiB: ["memory_mib", "mem_mib"],
  disk: ["storage_gb", "storage", "disk", "disk_gb"],
  pods: ["pods", "pod_count"],
  spec: ["spec", "vm_spec"],
  network: ["network", "bgp", "net"],
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

const normPeriod = (raw) => {
  const m = /^(\d{4})[-/](\d{1,2})$/.exec(raw.trim());
  if (!m) return null;
  const month = Number(m[2]);
  if (month < 1 || month > 12) return null;
  return `${m[1]}-${String(month).padStart(2, "0")}`;
};

const numAt = (v, { float = false } = {}) => {
  const s = (v ?? "").trim();
  if (s === "") return 0;
  const n = float ? parseFloat(s) : Number(s);
  if (!Number.isFinite(n) || n < 0) return null;
  return float ? n : Math.trunc(n);
};

/**
 * Parse CSV/TSV text into form-shaped demand entries.
 * @param text   raw pasted/uploaded text
 * @param specs  the VM Spec catalog ({name, ...}[]) for spec-name resolution
 * @returns {entries, errors: string[], warnings: string[], summary}
 */
export function parseDemandCsv(text, specs) {
  const errors = [], warnings = [];
  const delim = text.split("\n", 1)[0].includes("\t") ? "\t" : ",";
  const rows = parseDelimited(text, delim);
  if (rows.length < 2) {
    return { entries: [], errors: ["Need a header row and at least one data row."], warnings, summary: null };
  }

  // Header mapping.
  const colOf = {};   // canonical field -> column index
  const unknown = [];
  rows[0].forEach((h, idx) => {
    const key = h.trim().toLowerCase();
    const field = Object.keys(HEADER_ALIASES)
      .find((f) => HEADER_ALIASES[f].includes(key));
    if (!field) { if (key) unknown.push(h.trim()); return; }
    if (field in colOf) errors.push(`Duplicate column "${h.trim()}".`);
    colOf[field] = idx;
  });
  if (unknown.length) warnings.push(`Ignored unknown column(s): ${unknown.join(", ")}.`);
  if (!("cluster" in colOf)) errors.push('Missing required column "cluster".');
  if (!("period" in colOf)) errors.push('Missing required column "period".');
  if ("memGiB" in colOf && "memMiB" in colOf)
    errors.push("Provide memory_gib OR memory_mib, not both.");
  if (errors.length) return { entries: [], errors, warnings, summary: null };

  const specIdxByName = new Map(specs.map((s, i) => [s.name.trim().toLowerCase(), i]));
  const cell = (r, f) => (f in colOf ? (r[colOf[f]] ?? "").trim() : "");

  const entries = [];
  const rowOfKey = new Map();
  for (let li = 1; li < rows.length; li++) {
    const r = rows[li];
    const line = li + 1;   // human numbering incl. header
    const bad = (msg) => errors.push(`Row ${line}: ${msg}`);

    const cluster = cell(r, "cluster");
    if (!cluster) { bad("cluster is required."); continue; }
    const period = normPeriod(cell(r, "period"));
    if (!period) { bad(`bad period "${cell(r, "period")}" (expected YYYY-MM).`); continue; }

    const role = cell(r, "role").toLowerCase() || "worker";
    if (!ROLES.has(role)) { bad(`unknown role "${cell(r, "role")}" (valid: ${[...ROLES].join(", ")}).`); continue; }

    const cpu = numAt(cell(r, "cpu"));
    const disk = numAt(cell(r, "disk"));
    const pods = numAt(cell(r, "pods"));
    let memGiB;
    if ("memMiB" in colOf) {
      const mib = numAt(cell(r, "memMiB"));
      memGiB = mib == null ? null : +(mib / 1024).toFixed(2);
    } else {
      memGiB = numAt(cell(r, "memGiB"), { float: true });
    }
    if ([cpu, memGiB, disk, pods].some((v) => v == null)) {
      bad("numeric fields must be non-negative numbers.");
      continue;
    }

    let specIdx = -1;
    const specName = cell(r, "spec");
    if (specName) {
      specIdx = specIdxByName.get(specName.toLowerCase()) ?? -2;
      if (specIdx === -2) {
        bad(`unknown spec "${specName}" (catalog: ${specs.map((s) => s.name).join(", ") || "empty"}).`);
        continue;
      }
    }

    const fab = cell(r, "fab");
    const key = [fab, cluster, role, period].join("|");
    if (rowOfKey.has(key)) {
      bad(`duplicate (fab, cluster, role, period) — first seen at row ${rowOfKey.get(key)}.`);
      continue;
    }
    rowOfKey.set(key, line);

    entries.push({
      cluster_id: cluster, fab, period, node_role: role,
      cpu, memGiB, disk, pods, specIdx, network: cell(r, "network"),
    });
  }

  // Single-fab mode means ALL entries have fab="" — a mix is a request-level
  // input error later, so catch it here.
  const fabs = new Set(entries.map((e) => e.fab));
  if (fabs.has("") && fabs.size > 1)
    errors.push('Mixed blank and named "fab" values: leave fab blank on every row (single-fab) or name it on every row.');

  if (errors.length) return { entries: [], errors, warnings, summary: null };

  const periods = entries.map((e) => e.period).sort();
  return {
    entries, errors, warnings,
    summary: {
      entries: entries.length,
      fabs: new Set(entries.map((e) => e.fab || "(single fab)")).size,
      clusters: new Set(entries.map((e) => e.cluster_id)).size,
      from: periods[0], to: periods[periods.length - 1],
    },
  };
}

const csvField = (v) => {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
};

/** Export form-shaped demand entries back to canonical CSV. */
export function exportDemandCsv(entries, specs) {
  const header = "fab,cluster,role,period,cpu_cores,memory_gib,storage_gb,pods,spec,network";
  const lines = entries.map((e) => [
    e.fab, e.cluster_id, e.node_role, e.period,
    e.cpu || 0, e.memGiB || 0, e.disk || 0, e.pods || 0,
    specs[e.specIdx]?.name ?? "", e.network,
  ].map(csvField).join(","));
  return [header, ...lines].join("\n") + "\n";
}
