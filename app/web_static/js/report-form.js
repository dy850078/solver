/* Structured form for building a CapacityPlanRequest.
 *
 * The form is the source of truth for Run; the Advanced JSON editor syncs
 * both ways via explicit buttons. Memory is entered in GiB (users think in
 * GiB) and converted to memory_mib on build. In-stock machines are entered
 * as GROUPS (count × capacity @ fab/dc/ag/network) and expanded to
 * individual Baremetals on build; loading JSON re-groups them (machine ids
 * are regenerated — they are opaque to the planner).
 */

const $ = (id) => document.getElementById(id);
const GiB = 1024; // MiB per GiB

const state = {
  entries: [],
  stock: [],
  types: [],
  caps: [],
  committed: [],
};

const blank = {
  entries: () => ({ cluster_id: "cluster-1", fab: "", period: "", node_role: "worker",
                    cpu: 0, memGiB: 0, disk: 0, pods: 0,
                    specCpu: 8, specMemGiB: 16, specDisk: 100, network: "" }),
  stock: () => ({ count: 1, cpu: 64, memGiB: 256, disk: 2000, fab: "", dc: "dc-1",
                  ag: "ag-1", network: "" }),
  types: () => ({ type_id: "", cpu: 64, memGiB: 256, disk: 2000, fab: "" }),
  caps: () => ({ fab: "", bucket: "ag-1", network: "", max_bm: 1 }),
  committed: () => ({ type_id: "", count: 1, fab: "", bucket: "" }),
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

const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];

function entryRow(e, i) {
  const roleOpts = ROLES.map((r) =>
    `<option value="${r}"${r === e.node_role ? " selected" : ""}>${r}</option>`).join("");
  return `<div class="frow-card">
    <div class="frow-card__head">
      <span class="frow-card__title">#${i + 1}</span>
      ${delBtn("entries", i)}
    </div>
    <div class="frow frow--3">
      <label class="mini"><span class="mini__label">Month</span>
        <input class="input" type="month" value="${esc(e.period)}"
          data-s="entries" data-i="${i}" data-f="period"></label>
      ${txt("entries", i, "cluster_id", e.cluster_id, "Cluster")}
      ${txt("entries", i, "fab", e.fab, "Fab", "single-fab 留空")}
    </div>
    <div class="frow frow--4">
      ${num("entries", i, "cpu", e.cpu, "vCore")}
      ${num("entries", i, "memGiB", e.memGiB, "Mem GiB")}
      ${num("entries", i, "disk", e.disk, "Disk GB")}
      ${num("entries", i, "pods", e.pods, "Pods ≥")}
    </div>
    <div class="frow frow--4">
      <label class="mini"><span class="mini__label">Role</span>
        <select class="select" data-s="entries" data-i="${i}" data-f="node_role">${roleOpts}</select></label>
      ${num("entries", i, "specCpu", e.specCpu, "VM spec c")}
      ${num("entries", i, "specMemGiB", e.specMemGiB, "spec GiB")}
      ${num("entries", i, "specDisk", e.specDisk, "spec GB")}
    </div>
  </div>`;
}

function stockRow(g, i) {
  return `<div class="frow-card">
    <div class="frow-card__head">
      <span class="frow-card__title">×<b>${g.count}</b> @ ${esc(g.fab) || "—"}/${esc(g.ag)}</span>
      ${delBtn("stock", i)}
    </div>
    <div class="frow frow--4">
      ${num("stock", i, "count", g.count, "台數", { min: 1 })}
      ${num("stock", i, "cpu", g.cpu, "vCore/台")}
      ${num("stock", i, "memGiB", g.memGiB, "Mem GiB")}
      ${num("stock", i, "disk", g.disk, "Disk GB")}
    </div>
    <div class="frow frow--4">
      ${txt("stock", i, "fab", g.fab, "Fab (site)", "留空")}
      ${txt("stock", i, "dc", g.dc, "DC")}
      ${txt("stock", i, "ag", g.ag, "AG")}
      ${txt("stock", i, "network", g.network, "Network", "如 bgp1")}
    </div>
  </div>`;
}

function typeRow(t, i) {
  return `<div class="frow-card">
    <div class="frow frow--type">
      ${txt("types", i, "type_id", t.type_id, "Type id", "如 big-64c")}
      ${num("types", i, "cpu", t.cpu, "vCore")}
      ${num("types", i, "memGiB", t.memGiB, "GiB")}
      ${num("types", i, "disk", t.disk, "GB")}
      ${txt("types", i, "fab", t.fab, "Fab", "全域留空")}
      ${delBtn("types", i)}
    </div>
  </div>`;
}

function capRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--type">
      ${txt("caps", i, "fab", c.fab, "Fab")}
      ${txt("caps", i, "bucket", c.bucket, "Bucket (AG/DC)")}
      ${txt("caps", i, "network", c.network, "Network", "全桶留空")}
      ${num("caps", i, "max_bm", c.max_bm, "Max BM")}
      ${delBtn("caps", i)}
    </div>
  </div>`;
}

function committedRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--type">
      ${txt("committed", i, "type_id", c.type_id, "Type id")}
      ${num("committed", i, "count", c.count, "台數", { min: 1 })}
      ${txt("committed", i, "fab", c.fab, "Fab")}
      ${txt("committed", i, "bucket", c.bucket, "Bucket", "浮動留空")}
      ${delBtn("committed", i)}
    </div>
  </div>`;
}

const SECTIONS = {
  entries: { el: "demand-rows", row: entryRow },
  stock: { el: "stock-rows", row: stockRow },
  types: { el: "type-rows", row: typeRow },
  caps: { el: "cap-rows", row: capRow },
  committed: { el: "committed-rows", row: committedRow },
};

function renderSection(name) {
  const sec = SECTIONS[name];
  const host = $(sec.el);
  host.innerHTML = state[name].map((item, i) => sec.row(item, i)).join("")
    || `<p class="muted form-empty">（無）</p>`;
}

function renderAll() {
  for (const name of Object.keys(SECTIONS)) renderSection(name);
}

/* ── Build a CapacityPlanRequest from the form ── */

export function buildRequest() {
  const demand_book = state.entries.map((e) => ({
    cluster_id: e.cluster_id || "cluster-1",
    node_role: e.node_role,
    period: e.period,
    cpu_cores: +e.cpu || 0,
    memory_mib: Math.round((+e.memGiB || 0) * GiB),
    storage_gb: +e.disk || 0,
    pod_count: +e.pods || 0,
    vm_specs: (+e.specCpu || +e.specMemGiB || +e.specDisk) ? [{
      cpu_cores: +e.specCpu || 0,
      memory_mib: Math.round((+e.specMemGiB || 0) * GiB),
      storage_gb: +e.specDisk || 0,
    }] : null,
    fab: e.fab || "",
    network: e.network || "",
  }));

  let bmSeq = 0;
  const in_stock = state.stock.flatMap((g) =>
    Array.from({ length: +g.count || 0 }, () => {
      bmSeq += 1;
      return {
        id: `bm-${bmSeq}`,
        total_capacity: {
          cpu_cores: +g.cpu || 0,
          memory_mib: Math.round((+g.memGiB || 0) * GiB),
          storage_gb: +g.disk || 0,
        },
        topology: {
          site: g.fab || "", phase: "p1", datacenter: g.dc || "",
          room: "room-1", rack: `rack-${bmSeq}`, ag: g.ag || "",
        },
        network: g.network || "",
      };
    }));

  const req = {
    demand_book,
    in_stock,
    procurement_types: state.types
      .filter((t) => t.type_id)
      .map((t) => ({
        type_id: t.type_id,
        capacity: {
          cpu_cores: +t.cpu || 0,
          memory_mib: Math.round((+t.memGiB || 0) * GiB),
          storage_gb: +t.disk || 0,
        },
        fab: t.fab || "",
      })),
    procurement_caps: state.caps
      .filter((c) => c.bucket)
      .map((c) => ({ fab: c.fab || "", bucket: c.bucket,
                     network: c.network || "", max_bm: +c.max_bm || 0 })),
    committed_stock: state.committed
      .filter((c) => c.type_id)
      .map((c) => ({ type_id: c.type_id, count: +c.count || 0,
                     fab: c.fab || "", bucket: c.bucket || null })),
    config: {
      auto_generate_anti_affinity: $("cfg-autoaa").checked,
      max_pods_per_node: +$("cfg-maxpods").value || 0,
      procurement_spread_dimension: $("cfg-spread").value,
      fab_topology_dimension: "site",
    },
  };
  return req;
}

/* ── Populate the form from a CapacityPlanRequest JSON ── */

export function loadIntoForm(json) {
  state.entries = (json.demand_book || []).map((e) => {
    const spec = e.vm_specs?.[0];
    return {
      cluster_id: e.cluster_id || "", fab: e.fab || "", period: e.period || "",
      node_role: e.node_role || "worker",
      cpu: e.cpu_cores || 0,
      memGiB: +((e.memory_mib || 0) / GiB).toFixed(2),
      disk: e.storage_gb || 0,
      pods: e.pod_count || 0,
      specCpu: spec?.cpu_cores || 0,
      specMemGiB: +((spec?.memory_mib || 0) / GiB).toFixed(2),
      specDisk: spec?.storage_gb || 0,
      network: e.network || "",
    };
  });

  // Group individual BMs by (capacity, site, dc, ag, network); ids are
  // regenerated on build.
  const groups = new Map();
  for (const bm of json.in_stock || []) {
    const cap = bm.total_capacity || {};
    const topo = bm.topology || {};
    const key = [cap.cpu_cores, cap.memory_mib, cap.storage_gb,
                 topo.site, topo.datacenter, topo.ag, bm.network].join("|");
    if (!groups.has(key)) {
      groups.set(key, {
        count: 0,
        cpu: cap.cpu_cores || 0,
        memGiB: +((cap.memory_mib || 0) / GiB).toFixed(2),
        disk: cap.storage_gb || 0,
        fab: topo.site || "", dc: topo.datacenter || "",
        ag: topo.ag || "", network: bm.network || "",
      });
    }
    groups.get(key).count += 1;
  }
  state.stock = [...groups.values()];

  state.types = (json.procurement_types || []).map((t) => ({
    type_id: t.type_id || "",
    cpu: t.capacity?.cpu_cores || 0,
    memGiB: +((t.capacity?.memory_mib || 0) / GiB).toFixed(2),
    disk: t.capacity?.storage_gb || 0,
    fab: t.fab || "",
  }));

  state.caps = (json.procurement_caps || []).map((c) => ({
    fab: c.fab || "", bucket: c.bucket || "",
    network: c.network || "", max_bm: c.max_bm ?? 1,
  }));

  state.committed = (json.committed_stock || []).map((c) => ({
    type_id: c.type_id || "", count: c.count ?? 1,
    fab: c.fab || "", bucket: c.bucket || "",
  }));

  const cfg = json.config || {};
  $("cfg-maxpods").value = cfg.max_pods_per_node ?? 0;
  $("cfg-spread").value = cfg.procurement_spread_dimension || "ag";
  $("cfg-autoaa").checked = cfg.auto_generate_anti_affinity !== false;

  if (state.caps.length || state.committed.length) $("supply-adv").open = true;
  renderAll();
}

/* ── Wiring ── */

export function initForm() {
  // Field edits → state (delegated; numbers stay strings until build).
  document.querySelector(".sidebar").addEventListener("input", (ev) => {
    const t = ev.target;
    const { s, i, f } = t.dataset;
    if (!s || !SECTIONS[s]) return;
    state[s][+i][f] = t.type === "number" ? +t.value : t.value;
  });

  // Row remove (re-render the section).
  document.querySelector(".sidebar").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-del]");
    if (!btn) return;
    const s = btn.dataset.del;
    state[s].splice(+btn.dataset.i, 1);
    renderSection(s);
  });

  const adders = { "add-entry": "entries", "add-stock": "stock",
                   "add-type": "types", "add-cap": "caps",
                   "add-committed": "committed" };
  for (const [btnId, s] of Object.entries(adders)) {
    $(btnId).addEventListener("click", () => {
      const item = blank[s]();
      // New demand entries copy the previous row's context for fast entry.
      if (s === "entries" && state.entries.length) {
        Object.assign(item, structuredClone(
          state.entries[state.entries.length - 1]));
      }
      state[s].push(item);
      renderSection(s);
    });
  }

  // Form ⇄ JSON sync buttons.
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

  // Sensible starting point: one demand entry + one machine group + one type.
  state.entries = [blank.entries()];
  state.stock = [blank.stock()];
  state.types = [{ ...blank.types(), type_id: "big-64c" }];
  renderAll();
}
