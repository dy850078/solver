import test from "node:test";
import assert from "node:assert/strict";
import { JS, listExamples, prune, readJson } from "./helpers.mjs";

const { requestToSheets, sheetsToRequest, SHEETS, SETTINGS } = await import(JS + "demand-xlsx.js");

const roundTrip = (req) => sheetsToRequest(requestToSheets(req), {});
const sheet = (name, ...rows) => ({ name, rows });
const DEMANDS_HEADER = ["cluster", "node_role", "ip", "vm_spec", "count_as", "count"];
const VM_SPECS = sheet("VM specs", ["name", "cpu_cores", "memory_gib", "storage_gb", "gpu"],
  ["third", 30, 240, 2000, ""]);

// The workbook is "the request JSON in sheet form" (ADR-023): exporting any
// template and importing it back into an EMPTY form must reproduce it.
for (const file of listExamples("bm_demand", ".json")) {
  test(`round trip reproduces examples/bm_demand/${file}`, () => {
    const req = readJson("bm_demand/" + file);
    const out = roundTrip(req);
    assert.equal(out.errorCount, 0, JSON.stringify(out.errors));
    assert.deepEqual(out.warnings, []);
    assert.deepEqual(prune(out.request), prune(req));
    // and it is a fixed point: a second trip changes nothing at all
    assert.deepEqual(roundTrip(out.request).request, out.request);
  });
}

test("export headers are the importer's canonical column keys", () => {
  const sheets = requestToSheets(readJson("bm_demand/pm_three_clusters.json"));
  assert.deepEqual(sheets.map((s) => s.name),
    ["Demands", "VM specs", "Machine models", "Groups", "Settings"]);
  assert.deepEqual(sheets[0].rows[0], SHEETS.demands.columns.map((c) => c.key));
  assert.deepEqual(sheets[4].rows.slice(1).map((r) => r[0]), SETTINGS.map((s) => s.key));
});

test("sheet names, column names and order are forgiving", () => {
  const out = sheetsToRequest([
    sheet(" DEMAND ", ["Qty", "Spec", "Cluster ID", "Routable", "Unit"],
      [3, "third", "A1", "nr", "machines"]),
    VM_SPECS,
  ], {});
  assert.equal(out.errorCount, 0, JSON.stringify(out));
  assert.deepEqual(out.request.demands, [{
    cluster_id: "A1", node_role: "worker", ip_type: "non-routable",
    vm_spec: "third", bm_equivalents: 3,
  }]);
});

test("count_as=VMs maps to vm_count, never to bm_equivalents", () => {
  const out = sheetsToRequest([
    sheet("Demands", DEMANDS_HEADER, ["A1", "master", "R", "third", "VMs", 3]),
    VM_SPECS,
  ], {});
  assert.equal(out.errorCount, 0, JSON.stringify(out));
  const [d] = out.request.demands;
  assert.equal(d.vm_count, 3);
  assert.equal(d.bm_equivalents, undefined);
});

test("a missing Demands sheet blocks the import", () => {
  const out = sheetsToRequest([VM_SPECS], {});
  assert.ok(out.errorCount > 0);
  assert.match(out.errors.join("\n"), /required sheet "Demands" is missing/);
});

test("bad cells are reported on their row and block the import", () => {
  const out = sheetsToRequest([
    sheet("Demands", DEMANDS_HEADER,
      ["A1", "worker", "R", "third", "BMs", 2.5],        // not a whole number
      ["", "worker", "R", "third", "BMs", 2],            // cluster missing
      ["B1", "worker", "R", "nope", "BMs", 2]),          // unknown spec
    VM_SPECS,
  ], {});
  assert.ok(out.errorCount >= 3, JSON.stringify(out.sheets.demands.rows.map((r) => r.errors)));
  for (const row of out.sheets.demands.rows) assert.ok(row.errors.length > 0, JSON.stringify(row));
});

test("the same demand twice is an error (identity includes labels)", () => {
  const header = [...DEMANDS_HEADER, "labels"];
  const dup = sheetsToRequest([
    sheet("Demands", header,
      ["A1", "worker", "R", "third", "BMs", 2, ""],
      ["A1", "worker", "R", "third", "BMs", 3, ""]),
    VM_SPECS,
  ], {});
  assert.ok(dup.errorCount > 0);
  const distinct = sheetsToRequest([
    sheet("Demands", header,
      ["A1", "worker", "R", "third", "BMs", 2, "dedicated=AAA"],
      ["A1", "worker", "R", "third", "BMs", 3, "dedicated=BBB"]),
    VM_SPECS,
  ], {});
  assert.equal(distinct.errorCount, 0, JSON.stringify(distinct.sheets.demands.rows));
});

test("an absent optional sheet keeps the base section; an empty one clears it", () => {
  const base = readJson("bm_demand/pm_three_clusters.json");
  const demands = requestToSheets(base)[0];
  const specs = requestToSheets(base)[1];
  const kept = sheetsToRequest([demands, specs], base);
  assert.equal(kept.errorCount, 0, JSON.stringify(kept.errors));
  assert.deepEqual(kept.request.groups, base.groups);

  const cleared = sheetsToRequest(
    [demands, specs, sheet("Groups", ["name", "clusters", "bm_spec", "fleet_size"])], base);
  assert.deepEqual(cleared.request.groups, []);
});
