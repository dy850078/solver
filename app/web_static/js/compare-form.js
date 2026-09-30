// Compare set form — the DOM side of a CompareSet (see compare-set.js for
// the data side). Three catalogs (VM specs, BM models, bundles) reuse the
// Topology page's cards from mockform.js; the scenario list references them
// by name. `readSet()` turns the DOM back into the JSON document, and
// `renderForm(set)` does the reverse, so load / save / draft are lossless.

import {
  el, specRow, bmRow, groupRow, readGroupRow, readBmRows, readCapacityRows,
  refreshSpecDropdowns,
} from "./mockform.js";
import { modelsOf, scenarioAutoName } from "./compare-set.js";

const $ = (id) => document.getElementById(id);

const DEFAULT_GROUP = { role: "worker", count: 3, ip_type: "routable", spec: "", max_per_bm: "" };

let specRowsEl, modelRowsEl, bundleRowsEl, scenarioRowsEl;
let onChange = () => {};

/* ── catalog helpers ─────────────────────────────────────────────── */

function specNames() {
  return [...specRowsEl.querySelectorAll(".spec-name")].map((i) => i.value.trim()).filter(Boolean);
}
function modelNames() {
  return [...modelRowsEl.querySelectorAll(".bm-name")].map((i) => i.value.trim()).filter(Boolean);
}
function bundleNames() {
  return [...bundleRowsEl.querySelectorAll(".bundle-name")].map((i) => i.value.trim()).filter(Boolean);
}

// Spec catalog changed → every bundle's node-group spec dropdowns follow.
function syncSpecs() {
  const names = specNames();
  for (const rows of bundleRowsEl.querySelectorAll(".group-rows")) refreshSpecDropdowns(rows, names);
}

// Model / bundle names changed → scenario reference dropdowns follow.
function fillSelect(sel, names, current, { multi = null } = {}) {
  sel.innerHTML = "";
  if (multi) {
    // A multi-model scenario (from a loaded file) keeps its combined entry.
    sel.appendChild(el("option", { value: multi.join("+"), text: multi.join("+"), selected: true }));
    sel.dataset.multi = JSON.stringify(multi);
  } else {
    delete sel.dataset.multi;
  }
  for (const n of names) sel.appendChild(el("option", { value: n, text: n, selected: !multi && n === current }));
  if (!multi && current && !names.includes(current)) {
    sel.appendChild(el("option", { value: current, text: `${current} (missing)`, selected: true }));
  }
}
function syncRefs() {
  const models = modelNames();
  const bundles = bundleNames();
  for (const row of scenarioRowsEl.querySelectorAll(".sc-row")) {
    const m = row.querySelector(".sc-model");
    const multi = m.dataset.multi ? JSON.parse(m.dataset.multi) : null;
    fillSelect(m, models, m.value, { multi: multi && multi.every((x) => models.includes(x)) ? multi : null });
    const b = row.querySelector(".sc-bundle");
    fillSelect(b, bundles, b.value);
  }
  renderGridOptions();
}

/* ── bundle card ─────────────────────────────────────────────────── */

function bundleCard(name = "", groups = null) {
  const nameInput = el("input", { class: "input bundle-name", type: "text", placeholder: "bundle name", value: name, spellcheck: "false" });
  const rows = el("div", { class: "group-rows" },
    (groups && groups.length ? groups : [DEFAULT_GROUP]).map((g) => groupRow(g, { specNames: specNames() })));
  const add = el("button", { type: "button", class: "btn btn--ghost btn--small", text: "+ node group" });
  add.addEventListener("click", () => { rows.appendChild(groupRow(DEFAULT_GROUP, { specNames: specNames() })); onChange(); });
  const remove = el("button", { type: "button", class: "btn btn--ghost btn--small cap-remove", text: "✕", title: "Remove bundle" });
  const card = el("div", { class: "bundle-card" }, [
    el("div", { class: "bundle-card__head" }, [
      el("label", { class: "mini" }, [el("span", { class: "mini__label", text: "bundle name" }), nameInput]),
      remove,
    ]),
    rows,
    add,
  ]);
  remove.addEventListener("click", () => {
    if (bundleRowsEl.querySelectorAll(".bundle-card").length > 1) { card.remove(); syncRefs(); onChange(); }
  });
  return card;
}

/* ── scenario row ────────────────────────────────────────────────── */

function scenarioRow(sc = {}) {
  const models = modelNames();
  const bundles = bundleNames();
  const multi = Array.isArray(sc.bm_model) && sc.bm_model.length > 1 ? sc.bm_model : null;
  const on = el("input", { type: "checkbox", class: "sc-on", checked: sc.enabled !== false, title: "Include in Run all" });
  const name = el("input", { class: "input sc-name", type: "text", placeholder: "scenario name", value: sc.name || "", spellcheck: "false" });
  const model = el("select", { class: "select sc-model", title: "BM model" });
  fillSelect(model, models, multi ? null : (Array.isArray(sc.bm_model) ? sc.bm_model[0] : sc.bm_model) || models[0], { multi });
  const bundle = el("select", { class: "select sc-bundle", title: "Bundle" });
  fillSelect(bundle, bundles, sc.bundle || bundles[0]);
  const clusters = el("input", { class: "input sc-clusters", type: "number", min: 1, value: sc.clusters ?? 1, title: "Cluster count" });
  const hasOv = sc.overrides && Object.keys(sc.overrides).length > 0;
  const ovBtn = el("button", { type: "button", class: `x sc-ov-btn btn btn--ghost${hasOv ? " x--on" : ""}`, text: "⚙", title: "Overrides: any mock knob for this scenario only (JSON)" });
  const dup = el("button", { type: "button", class: "x btn btn--ghost", text: "⧉", title: "Duplicate" });
  const del = el("button", { type: "button", class: "x btn btn--ghost", text: "✕", title: "Remove" });
  const ov = el("textarea", { class: "editor sc-overrides", spellcheck: "false",
    placeholder: '{ "tightness": 0.7, "failover": true }',
    value: hasOv ? JSON.stringify(sc.overrides, null, 1) : "" });
  const ovWrap = el("div", { class: `sc-row__ov${hasOv ? "" : " hidden"}` }, [ov]);
  const status = el("div", { class: "sc-row__status" });
  const row = el("div", { class: `sc-row${sc.enabled === false ? " sc-row--off" : ""}` }, [
    el("div", { class: "sc-row__top" }, [on, name, ovBtn, dup, del]),
    el("div", { class: "sc-row__refs" }, [model, bundle, clusters]),
    ovWrap,
    status,
  ]);
  // Auto-name follows the references until the user types a name.
  let userNamed = !!sc.name && sc.name !== scenarioAutoName({ bm_model: sc.bm_model, bundle: sc.bundle, clusters: sc.clusters ?? 1 });
  const autoName = () => {
    if (userNamed) return;
    name.value = scenarioAutoName({ bm_model: multi || model.value, bundle: bundle.value, clusters: Number(clusters.value) || 1 });
  };
  if (!sc.name) autoName();
  name.addEventListener("input", () => { userNamed = name.value.trim() !== ""; });
  for (const c of [model, bundle, clusters]) c.addEventListener("change", autoName);
  on.addEventListener("change", () => row.classList.toggle("sc-row--off", !on.checked));
  ovBtn.addEventListener("click", () => { ovWrap.classList.toggle("hidden"); ov.focus(); });
  ov.addEventListener("input", () => ovBtn.classList.toggle("x--on", ov.value.trim() !== ""));
  dup.addEventListener("click", () => {
    const copy = readScenarioRow(row, { lenient: true });
    copy.name = `${copy.name} (copy)`;
    row.after(scenarioRow(copy));
    onChange();
  });
  del.addEventListener("click", () => { row.remove(); onChange(); });
  return row;
}

function readScenarioRow(row, { lenient = false } = {}) {
  const model = row.querySelector(".sc-model");
  const bm_model = model.dataset.multi ? JSON.parse(model.dataset.multi) : model.value;
  const sc = {
    name: row.querySelector(".sc-name").value.trim(),
    bm_model,
    bundle: row.querySelector(".sc-bundle").value,
    clusters: Number(row.querySelector(".sc-clusters").value) || 1,
    enabled: row.querySelector(".sc-on").checked,
  };
  const raw = row.querySelector(".sc-overrides").value.trim();
  if (raw) {
    try {
      sc.overrides = JSON.parse(raw);
    } catch (err) {
      if (!lenient) throw new Error(`Scenario "${sc.name || "?"}": overrides JSON — ${err.message}`);
    }
  }
  if (!sc.name) sc.name = scenarioAutoName(sc);
  return sc;
}

/* ── grid generator ──────────────────────────────────────────────── */

function renderGridOptions() {
  const box = (container, names) => {
    const checked = new Set([...container.querySelectorAll("input:checked")].map((i) => i.value));
    container.innerHTML = "";
    for (const n of names) {
      container.appendChild(el("label", {}, [
        el("input", { type: "checkbox", value: n, checked: checked.size ? checked.has(n) : true }),
        el("span", { text: n }),
      ]));
    }
  };
  box($("grid-models"), modelNames());
  box($("grid-bundles"), bundleNames());
}

function addGridCombinations() {
  const models = [...$("grid-models").querySelectorAll("input:checked")].map((i) => i.value);
  const bundles = [...$("grid-bundles").querySelectorAll("input:checked")].map((i) => i.value);
  const counts = [...new Set($("grid-clusters").value.split(",").map((s) => Number(s.trim())).filter((n) => n >= 1))];
  const existing = new Set([...scenarioRowsEl.querySelectorAll(".sc-row")].map((r) => {
    const sc = readScenarioRow(r, { lenient: true });
    return `${modelsOf(sc).join("+")}|${sc.bundle}|${sc.clusters}`;
  }));
  let added = 0;
  for (const m of models) for (const b of bundles) for (const c of counts) {
    if (existing.has(`${m}|${b}|${c}`)) continue;
    scenarioRowsEl.appendChild(scenarioRow({ bm_model: m, bundle: b, clusters: c }));
    added += 1;
  }
  $("grid-hint").textContent = added ? `Added ${added} scenario${added === 1 ? "" : "s"}.` : "Every combination already exists.";
  onChange();
}

/* ── defaults ────────────────────────────────────────────────────── */

const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
const setVal = (id, v) => { $(id).value = v == null ? "" : v; };

function readDefaults() {
  const d = {
    racks: num("df-racks") ?? 4,
    ags: num("df-ags") ?? 3,
    anti_affinity: $("df-aa").checked,
    target_spread: { ag: num("df-spread-ag") ?? 3 },
    failover: $("df-failover").checked,
    tightness: num("df-tightness") ?? 1.0,
  };
  for (const k of ["sites", "phases", "datacenters", "rooms"]) {
    const v = num(`df-${k}`);
    if (v != null && v !== 1) d[k] = v;
  }
  const seed = num("df-seed");
  if (seed != null) d.seed = seed;
  const solve = num("df-solve");
  if (solve != null) d.config_overrides = { max_solve_time_seconds: solve };
  const advRaw = $("df-advanced").value.trim();
  if (advRaw) {
    let adv;
    try { adv = JSON.parse(advRaw); } catch (err) { throw new Error(`Advanced defaults JSON — ${err.message}`); }
    for (const [k, v] of Object.entries(adv)) {
      d[k] = (v && typeof v === "object" && !Array.isArray(v) && d[k] && typeof d[k] === "object")
        ? { ...d[k], ...v } : v;
    }
  }
  return d;
}

const FIELD_KEYS = new Set(["racks", "ags", "anti_affinity", "target_spread", "failover", "tightness",
  "sites", "phases", "datacenters", "rooms", "seed", "config_overrides"]);

function renderDefaults(d = {}) {
  setVal("df-racks", d.racks ?? 4);
  setVal("df-ags", d.ags ?? 3);
  $("df-aa").checked = d.anti_affinity ?? true;
  $("df-failover").checked = !!d.failover;
  setVal("df-spread-ag", d.target_spread?.ag ?? 3);
  setVal("df-tightness", d.tightness ?? 1.0);
  for (const k of ["sites", "phases", "datacenters", "rooms"]) setVal(`df-${k}`, d[k] && d[k] !== 1 ? d[k] : "");
  setVal("df-seed", d.seed ?? "");
  const co = { ...(d.config_overrides || {}) };
  setVal("df-solve", co.max_solve_time_seconds ?? "");
  delete co.max_solve_time_seconds;
  // Anything the fields can't hold → Advanced box, nothing silently lost.
  const adv = {};
  for (const [k, v] of Object.entries(d)) if (!FIELD_KEYS.has(k)) adv[k] = v;
  if (d.target_spread && Object.keys(d.target_spread).some((k) => k !== "ag")) adv.target_spread = d.target_spread;
  if (Object.keys(co).length) adv.config_overrides = co;
  $("df-advanced").value = Object.keys(adv).length ? JSON.stringify(adv, null, 2) : "";
}

/* ── public API ──────────────────────────────────────────────────── */

export function initForm({ onChange: cb }) {
  onChange = cb || (() => {});
  specRowsEl = $("spec-rows");
  modelRowsEl = $("model-rows");
  bundleRowsEl = $("bundle-rows");
  scenarioRowsEl = $("scenario-rows");

  $("spec-add").addEventListener("click", () => { specRowsEl.appendChild(specRow({}, { onNameInput: syncSpecs })); onChange(); });
  $("model-add").addEventListener("click", () => { modelRowsEl.appendChild(bmRow({})); syncRefs(); onChange(); });
  $("bundle-add").addEventListener("click", () => { bundleRowsEl.appendChild(bundleCard("", null)); syncRefs(); onChange(); });
  $("scenario-add").addEventListener("click", () => { scenarioRowsEl.appendChild(scenarioRow({})); onChange(); });
  $("grid-toggle").addEventListener("click", () => { renderGridOptions(); $("grid-panel").classList.toggle("hidden"); });
  $("grid-add").addEventListener("click", addGridCombinations);

  // Catalog name edits ripple into the scenario dropdowns; removals of
  // model/bundle cards go through the cards' own ✕ (bmRow's is parent-based).
  const sidebar = document.querySelector(".sidebar");
  sidebar.addEventListener("input", (e) => {
    if (e.target.matches(".bm-name, .bundle-name")) syncRefs();
    onChange();
  });
  sidebar.addEventListener("change", () => onChange());
  modelRowsEl.addEventListener("click", (e) => { if (e.target.matches(".cap-remove")) { setTimeout(syncRefs, 0); onChange(); } });
}

export function renderForm(set) {
  setVal("set-name", set.name || "");
  specRowsEl.innerHTML = "";
  for (const [name, cap] of Object.entries(set.vm_specs || {})) {
    specRowsEl.appendChild(specRow({ name, ...cap }, { onNameInput: syncSpecs }));
  }
  if (!specRowsEl.children.length) specRowsEl.appendChild(specRow({}, { onNameInput: syncSpecs }));
  modelRowsEl.innerHTML = "";
  for (const [name, m] of Object.entries(set.bm_models || {})) {
    modelRowsEl.appendChild(bmRow({ name, ...m.capacity, gpu: m.capacity?.gpu ?? {}, roles: m.roles || [] }));
  }
  if (!modelRowsEl.children.length) modelRowsEl.appendChild(bmRow({}));
  bundleRowsEl.innerHTML = "";
  for (const [name, groups] of Object.entries(set.bundles || {})) bundleRowsEl.appendChild(bundleCard(name, groups));
  if (!bundleRowsEl.children.length) bundleRowsEl.appendChild(bundleCard("", null));
  renderDefaults(set.defaults || {});
  scenarioRowsEl.innerHTML = "";
  for (const sc of set.scenarios || []) scenarioRowsEl.appendChild(scenarioRow(sc));
  syncSpecs();
  syncRefs();
}

// DOM → CompareSet. Throws Error with a user-facing message on bad JSON or a
// dangling reference; the backend re-validates everything anyway.
export function readSet() {
  const vm_specs = {};
  for (const s of readCapacityRows(specRowsEl, ".spec-row", "spec", false)) vm_specs[s.name] = s.cap;
  const bm_models = {};
  for (const p of readBmRows(modelRowsEl)) {
    if (!p.name) continue;
    bm_models[p.name] = { capacity: p.capacity, roles: p.roles || [] };
  }
  const bundles = {};
  for (const card of bundleRowsEl.querySelectorAll(".bundle-card")) {
    const name = card.querySelector(".bundle-name").value.trim();
    const groups = [...card.querySelectorAll(".group-row")].map(readGroupRow).filter(Boolean);
    if (!name && !groups.length) continue;
    if (!name) throw new Error("Every bundle needs a name.");
    if (!groups.length) throw new Error(`Bundle "${name}" has no node groups with count > 0.`);
    bundles[name] = groups;
  }
  const scenarios = [...scenarioRowsEl.querySelectorAll(".sc-row")].map((r) => readScenarioRow(r));
  const names = new Set();
  for (const sc of scenarios) {
    if (names.has(sc.name)) throw new Error(`Duplicate scenario name "${sc.name}".`);
    names.add(sc.name);
    for (const m of modelsOf(sc)) if (!(m in bm_models)) throw new Error(`Scenario "${sc.name}" references BM model "${m}", which is not in the catalog.`);
    if (!(sc.bundle in bundles)) throw new Error(`Scenario "${sc.name}" references bundle "${sc.bundle}", which is not in the catalog.`);
  }
  if (!Object.keys(bm_models).length) throw new Error("Add at least one BM model.");
  if (!Object.keys(bundles).length) throw new Error("Add at least one bundle.");
  return { name: $("set-name").value.trim(), vm_specs, bm_models, bundles, defaults: readDefaults(), scenarios };
}

// Result status shown inline on the scenario row (compare.js drives it).
export function setScenarioStatus(name, html) {
  for (const row of scenarioRowsEl.querySelectorAll(".sc-row")) {
    if (row.querySelector(".sc-name").value.trim() === name) row.querySelector(".sc-row__status").innerHTML = html;
  }
}
export function clearScenarioStatus() {
  for (const s of scenarioRowsEl.querySelectorAll(".sc-row__status")) s.innerHTML = "";
}
