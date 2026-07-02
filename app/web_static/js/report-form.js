/* Structured form for building a CapacityPlanRequest.
 *
 * Catalog-first UX: VM Specs and BM Models are defined once as named
 * catalogs; demand entries, in-stock groups and committed stock then just
 * SELECT from them instead of retyping sizes. The form is the source of
 * truth for Run; the Advanced JSON editor syncs both ways via explicit
 * buttons.
 *
 * Memory is entered in GiB and converted to memory_mib on build. In-stock
 * machines are entered as GROUPS (count × model @ fab/dc/ag/network) and
 * expanded to individual Baremetals on build; loading JSON re-groups them
 * (machine ids are regenerated — they are opaque to the planner).
 *
 * Terminology note: the UI says "BM Model"; the wire contract keeps the
 * existing field names (procurement_types / type_id).
 */

const $ = (id) => document.getElementById(id);
const GiB = 1024; // MiB per GiB

const state = {
  specs: [],     // {name, cpu, memGiB, disk}
  models: [],    // {model_id, cpu, memGiB, disk, fab, buyable}
  entries: [],   // {..., specIdx: -1 = any spec from the catalog}
  stock: [],     // {count, modelIdx, fab, dc, ag, network}
  caps: [],
  committed: [], // {modelIdx, count, fab, bucket}
};

const blank = {
  specs: () => ({ name: "", cpu: 8, memGiB: 16, disk: 100 }),
  models: () => ({ model_id: "", cpu: 64, memGiB: 256, disk: 2000, fab: "", buyable: true }),
  entries: () => ({ cluster_id: "cluster-1", fab: "", period: "", node_role: "worker",
                    cpu: 0, memGiB: 0, disk: 0, pods: 0, specIdx: 0, network: "" }),
  stock: () => ({ count: 1, modelIdx: 0, fab: "", dc: "dc-1", ag: "ag-1", network: "" }),
  caps: () => ({ fab: "", bucket: "ag-1", network: "", max_bm: 1 }),
  committed: () => ({ modelIdx: 0, count: 1, fab: "", bucket: "" }),
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

function specSelect(section, i, field, value, label, { withAny = false } = {}) {
  const opts = [];
  if (withAny) {
    opts.push(`<option value="-1"${value === -1 ? " selected" : ""}>Any (solver picks)</option>`);
  }
  state.specs.forEach((s, si) => {
    opts.push(`<option value="${si}"${value === si ? " selected" : ""}>${esc(specLabel(s))}</option>`);
  });
  return `<label class="mini"><span class="mini__label">${label}</span>
    <select class="select" data-s="${section}" data-i="${i}" data-f="${field}">${opts.join("")}</select></label>`;
}

function modelSelect(section, i, field, value, label) {
  const opts = state.models.map((m, mi) =>
    `<option value="${mi}"${value === mi ? " selected" : ""}>${esc(modelLabel(m))}</option>`);
  if (!opts.length) opts.push(`<option value="-1">— no models —</option>`);
  return `<label class="mini"><span class="mini__label">${label}</span>
    <select class="select" data-s="${section}" data-i="${i}" data-f="${field}">${opts.join("")}</select></label>`;
}

const specLabel = (s) =>
  `${s.name || "spec"} · ${s.cpu}c/${s.memGiB}g/${s.disk}gb`;
const modelLabel = (m) =>
  `${m.model_id || "model"} · ${m.cpu}c/${m.memGiB}g/${m.disk}gb`;

const ROLES = ["worker", "master", "learner", "infra", "l4lb-storage", "bastion"];

function specRow(s, i) {
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${txt("specs", i, "name", s.name, "Name", "e.g. M-8c16g")}
      ${num("specs", i, "cpu", s.cpu, "vCore")}
      ${num("specs", i, "memGiB", s.memGiB, "Mem GiB")}
      ${num("specs", i, "disk", s.disk, "Disk GB")}
      ${delBtn("specs", i)}
    </div>
  </div>`;
}

function modelRow(m, i) {
  return `<div class="frow-card">
    <div class="frow frow--spec">
      ${txt("models", i, "model_id", m.model_id, "Model id", "e.g. big-64c")}
      ${num("models", i, "cpu", m.cpu, "vCore")}
      ${num("models", i, "memGiB", m.memGiB, "Mem GiB")}
      ${num("models", i, "disk", m.disk, "Disk GB")}
      ${delBtn("models", i)}
    </div>
    <div class="frow frow--model">
      ${txt("models", i, "fab", m.fab, "Fab", "blank = all fabs")}
      <label class="mini mini--check" title="Can be purchased">
        <input type="checkbox" ${m.buyable ? "checked" : ""}
          data-s="models" data-i="${i}" data-f="buyable">
        <span class="mini__label">Buyable</span>
      </label>
    </div>
  </div>`;
}

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
      ${txt("entries", i, "fab", e.fab, "Fab", "blank = single-fab")}
    </div>
    <div class="frow frow--4">
      ${num("entries", i, "cpu", e.cpu, "vCore")}
      ${num("entries", i, "memGiB", e.memGiB, "Mem GiB")}
      ${num("entries", i, "disk", e.disk, "Disk GB")}
      ${num("entries", i, "pods", e.pods, "Pods ≥")}
    </div>
    <div class="frow frow--2">
      <label class="mini"><span class="mini__label">Role</span>
        <select class="select" data-s="entries" data-i="${i}" data-f="node_role">${roleOpts}</select></label>
      ${specSelect("entries", i, "specIdx", e.specIdx, "VM spec", { withAny: true })}
    </div>
  </div>`;
}

function stockRow(g, i) {
  return `<div class="frow-card">
    <div class="frow frow--stock">
      ${num("stock", i, "count", g.count, "Machines", { min: 1, step: 1 })}
      ${modelSelect("stock", i, "modelIdx", g.modelIdx, "BM model")}
      ${delBtn("stock", i)}
    </div>
    <div class="frow frow--4">
      ${txt("stock", i, "fab", g.fab, "Fab (site)", "blank")}
      ${txt("stock", i, "dc", g.dc, "DC")}
      ${txt("stock", i, "ag", g.ag, "AG")}
      ${txt("stock", i, "network", g.network, "Network", "e.g. bgp1")}
    </div>
  </div>`;
}

function capRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--cap">
      ${txt("caps", i, "fab", c.fab, "Fab")}
      ${txt("caps", i, "bucket", c.bucket, "Bucket (AG/DC)")}
      ${txt("caps", i, "network", c.network, "Network", "blank = whole bucket")}
      ${num("caps", i, "max_bm", c.max_bm, "Max BM", { step: 1 })}
      ${delBtn("caps", i)}
    </div>
  </div>`;
}

function committedRow(c, i) {
  return `<div class="frow-card">
    <div class="frow frow--committed">
      ${modelSelect("committed", i, "modelIdx", c.modelIdx, "BM model")}
      ${num("committed", i, "count", c.count, "Count", { min: 1, step: 1 })}
      ${txt("committed", i, "fab", c.fab, "Fab")}
      ${txt("committed", i, "bucket", c.bucket, "Bucket", "blank = floating")}
      ${delBtn("committed", i)}
    </div>
  </div>`;
}

const SECTIONS = {
  specs: { el: "spec-rows", row: specRow, deps: ["entries"] },
  models: { el: "model-rows", row: modelRow, deps: ["stock", "committed"] },
  entries: { el: "demand-rows", row: entryRow, deps: [] },
  stock: { el: "stock-rows", row: stockRow, deps: [] },
  caps: { el: "cap-rows", row: capRow, deps: [] },
  committed: { el: "committed-rows", row: committedRow, deps: [] },
};

function renderSection(name) {
  const sec = SECTIONS[name];
  const host = $(sec.el);
  host.innerHTML = state[name].map((item, i) => sec.row(item, i)).join("")
    || `<p class="muted form-empty">(none)</p>`;
}

function renderAll() {
  for (const name of Object.keys(SECTIONS)) renderSection(name);
}

/* Catalog row removed → fix up references in dependents. */
function fixupIndexes(field, removed) {
  const fix = (v) => (v === removed ? -1 : v > removed ? v - 1 : v);
  if (field === "specIdx") for (const e of state.entries) e.specIdx = fix(e.specIdx);
  if (field === "modelIdx") {
    for (const g of state.stock) g.modelIdx = Math.max(0, fix(g.modelIdx));
    for (const c of state.committed) c.modelIdx = Math.max(0, fix(c.modelIdx));
  }
}

/* ── Build a CapacityPlanRequest from the form ── */

const toResources = (o) => ({
  cpu_cores: +o.cpu || 0,
  memory_mib: Math.round((+o.memGiB || 0) * GiB),
  storage_gb: +o.disk || 0,
});

export function buildRequest() {
  const demand_book = state.entries.map((e) => {
    const spec = state.specs[e.specIdx];
    return {
      cluster_id: e.cluster_id || "cluster-1",
      node_role: e.node_role,
      period: e.period,
      cpu_cores: +e.cpu || 0,
      memory_mib: Math.round((+e.memGiB || 0) * GiB),
      storage_gb: +e.disk || 0,
      pod_count: +e.pods || 0,
      // A specific spec pins the entry; "Any" (null) falls back to
      // config.vm_specs — the whole catalog — so the solver chooses.
      vm_specs: spec ? [toResources(spec)] : null,
      fab: e.fab || "",
      network: e.network || "",
    };
  });

  let bmSeq = 0;
  const in_stock = state.stock.flatMap((g) => {
    const model = state.models[g.modelIdx];
    if (!model) return [];
    return Array.from({ length: +g.count || 0 }, () => {
      bmSeq += 1;
      return {
        id: `bm-${bmSeq}`,
        total_capacity: toResources(model),
        topology: {
          site: g.fab || "", phase: "p1", datacenter: g.dc || "",
          room: "room-1", rack: `rack-${bmSeq}`, ag: g.ag || "",
        },
        network: g.network || "",
      };
    });
  });

  return {
    demand_book,
    in_stock,
    procurement_types: state.models
      .filter((m) => m.buyable && m.model_id)
      .map((m) => ({ type_id: m.model_id, capacity: toResources(m), fab: m.fab || "" })),
    procurement_caps: state.caps
      .filter((c) => c.bucket)
      .map((c) => ({ fab: c.fab || "", bucket: c.bucket,
                     network: c.network || "", max_bm: +c.max_bm || 0 })),
    committed_stock: state.committed
      .filter((c) => state.models[c.modelIdx]?.model_id)
      .map((c) => ({ type_id: state.models[c.modelIdx].model_id,
                     count: +c.count || 0, fab: c.fab || "",
                     bucket: c.bucket || null })),
    config: {
      auto_generate_anti_affinity: $("cfg-autoaa").checked,
      max_pods_per_node: +$("cfg-maxpods").value || 0,
      procurement_spread_dimension: $("cfg-spread").value,
      fab_topology_dimension: "site",
      // The whole spec catalog: the pool "Any" entries draw from.
      vm_specs: state.specs.map(toResources),
    },
  };
}

/* ── Populate the form from a CapacityPlanRequest JSON ── */

const fromMib = (mib) => +((mib || 0) / GiB).toFixed(2);
const resKey = (r) => `${r?.cpu_cores || 0}|${r?.memory_mib || 0}|${r?.storage_gb || 0}`;
const autoName = (r, suffix = "") =>
  `${r.cpu_cores || 0}c-${fromMib(r.memory_mib)}g${suffix}`;

export function loadIntoForm(json) {
  // 1. VM spec catalog: config.vm_specs first, then any per-entry specs.
  state.specs = [];
  const specIdxByKey = new Map();
  const addSpec = (r, name) => {
    const key = resKey(r);
    if (specIdxByKey.has(key)) return specIdxByKey.get(key);
    state.specs.push({
      name: name || autoName(r),
      cpu: r.cpu_cores || 0, memGiB: fromMib(r.memory_mib), disk: r.storage_gb || 0,
    });
    specIdxByKey.set(key, state.specs.length - 1);
    return state.specs.length - 1;
  };
  for (const r of json.config?.vm_specs || []) addSpec(r);

  state.entries = (json.demand_book || []).map((e) => {
    const spec = e.vm_specs?.[0];
    return {
      cluster_id: e.cluster_id || "", fab: e.fab || "", period: e.period || "",
      node_role: e.node_role || "worker",
      cpu: e.cpu_cores || 0,
      memGiB: fromMib(e.memory_mib),
      disk: e.storage_gb || 0,
      pods: e.pod_count || 0,
      specIdx: spec ? addSpec(spec) : -1,
      network: e.network || "",
    };
  });

  // 2. BM model catalog: buyable models from procurement_types; in-stock
  //    capacities that match none become non-buyable legacy models.
  state.models = [];
  const modelIdxByKey = new Map();
  const addModel = (cap, { model_id = "", fab = "", buyable = false } = {}) => {
    const key = resKey(cap);
    if (modelIdxByKey.has(key)) {
      const idx = modelIdxByKey.get(key);
      if (buyable) state.models[idx].buyable = true;
      if (model_id) state.models[idx].model_id = model_id;
      return idx;
    }
    state.models.push({
      model_id: model_id || autoName(cap, "-bm"),
      cpu: cap.cpu_cores || 0, memGiB: fromMib(cap.memory_mib),
      disk: cap.storage_gb || 0, fab, buyable,
    });
    modelIdxByKey.set(key, state.models.length - 1);
    return state.models.length - 1;
  };
  for (const t of json.procurement_types || []) {
    addModel(t.capacity || {}, { model_id: t.type_id, fab: t.fab || "", buyable: true });
  }

  // 3. In-stock: group by (capacity, site, dc, ag, network); ids regenerate.
  const groups = new Map();
  for (const bm of json.in_stock || []) {
    const cap = bm.total_capacity || {};
    const topo = bm.topology || {};
    const key = [resKey(cap), topo.site, topo.datacenter, topo.ag, bm.network].join("|");
    if (!groups.has(key)) {
      groups.set(key, {
        count: 0, modelIdx: addModel(cap),
        fab: topo.site || "", dc: topo.datacenter || "",
        ag: topo.ag || "", network: bm.network || "",
      });
    }
    groups.get(key).count += 1;
  }
  state.stock = [...groups.values()];

  state.caps = (json.procurement_caps || []).map((c) => ({
    fab: c.fab || "", bucket: c.bucket || "",
    network: c.network || "", max_bm: c.max_bm ?? 1,
  }));

  const modelIdxById = new Map(state.models.map((m, i) => [m.model_id, i]));
  state.committed = (json.committed_stock || []).map((c) => ({
    modelIdx: modelIdxById.get(c.type_id) ?? 0,
    count: c.count ?? 1, fab: c.fab || "", bucket: c.bucket || "",
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
  const sidebar = document.querySelector(".sidebar");

  // Field edits → state.
  sidebar.addEventListener("input", (ev) => {
    const t = ev.target;
    const { s, i, f } = t.dataset;
    if (!s || !SECTIONS[s]) return;
    let v;
    if (t.type === "checkbox") v = t.checked;
    else if (f.endsWith("Idx")) v = parseInt(t.value, 10);
    else if (t.type === "number") v = +t.value;
    else v = t.value;
    state[s][+i][f] = v;
  });

  // Catalog edits refresh the selects that reference them (on blur/commit,
  // so typing a name doesn't lose focus).
  sidebar.addEventListener("change", (ev) => {
    const { s } = ev.target.dataset;
    if (!s || !SECTIONS[s]?.deps?.length) return;
    for (const dep of SECTIONS[s].deps) renderSection(dep);
  });

  // Row remove.
  sidebar.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-del]");
    if (!btn) return;
    const s = btn.dataset.del;
    const i = +btn.dataset.i;
    state[s].splice(i, 1);
    if (s === "specs") fixupIndexes("specIdx", i);
    if (s === "models") fixupIndexes("modelIdx", i);
    renderSection(s);
    for (const dep of SECTIONS[s].deps || []) renderSection(dep);
  });

  const adders = { "add-spec": "specs", "add-model": "models",
                   "add-entry": "entries", "add-stock": "stock",
                   "add-cap": "caps", "add-committed": "committed" };
  for (const [btnId, s] of Object.entries(adders)) {
    $(btnId).addEventListener("click", () => {
      const item = blank[s]();
      // New demand entries copy the previous row for fast month-over-month
      // entry.
      if (s === "entries" && state.entries.length) {
        Object.assign(item, structuredClone(
          state.entries[state.entries.length - 1]));
      }
      state[s].push(item);
      renderSection(s);
      for (const dep of SECTIONS[s].deps || []) renderSection(dep);
    });
  }

  // Form ⇄ JSON sync.
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

  // Sensible starting point.
  state.specs = [
    { name: "S-4c8g", cpu: 4, memGiB: 8, disk: 100 },
    { name: "M-8c16g", cpu: 8, memGiB: 16, disk: 200 },
    { name: "L-16c32g", cpu: 16, memGiB: 32, disk: 400 },
  ];
  state.models = [
    { model_id: "std-64c", cpu: 64, memGiB: 256, disk: 2000, fab: "", buyable: true },
  ];
  state.entries = [{ ...blank.entries(), specIdx: 1 }];
  state.stock = [blank.stock()];
  renderAll();
}
