import test from "node:test";
import assert from "node:assert/strict";
import { JS, readText } from "./helpers.mjs";

const demand = await import(JS + "demand-csv.js");
const stock = await import(JS + "stock-csv.js");

const SPECS = [{ name: "small" }];
const MODELS = [{ model_id: "std-64c" }];

test("demand book sample imports clean and exports byte-identical", () => {
  const text = readText("capacity/demand_book_sample.csv");
  const out = demand.parseDemandCsv(text, SPECS, MODELS);
  assert.deepEqual(out.errors, []);
  assert.ok(out.entries.length > 0);
  assert.equal(demand.exportDemandCsv(out.entries, SPECS), text);
});

test("demand import is all-or-nothing: one bad row yields no entries", () => {
  const out = demand.parseDemandCsv(
    "cluster\tperiod\tspec\tbogus\n" +
    "c1\t2026-13\tnope\t1\n" +      // bad month + unknown spec
    "c2\t2026-01\t\t\n" +
    "c2\t2026-01\t\t\n",            // duplicate key
    SPECS, MODELS);
  assert.deepEqual(out.entries, []);
  assert.equal(out.errors.length, 3);
  assert.match(out.errors[0], /^Row 2: bad period/);
  assert.match(out.errors[1], /^Row 2: unknown spec "nope"/);
  assert.match(out.errors[2], /^Row 4: duplicate/);
  assert.deepEqual(out.warnings, ["Ignored unknown column(s): bogus."]);
});

test("demand header rules: required columns, one memory unit", () => {
  assert.match(demand.parseDemandCsv("period\n2026-01\n", SPECS).errors.join(), /Missing required column "cluster"/);
  assert.match(demand.parseDemandCsv("cluster\nc1\n", SPECS).errors.join(), /Missing required column "period"/);
  assert.deepEqual(
    demand.parseDemandCsv("cluster,period,memory_gib,memory_mib\nc,2026-01,1,1024\n", SPECS).errors,
    ["Provide memory_gib OR memory_mib, not both."]);
});

test("demand: an allowed model outside the catalog is rejected", () => {
  const out = demand.parseDemandCsv(
    "cluster,period,allowed_bm_types\nc1,2026-01,std-64c; ghost\n", SPECS, MODELS);
  assert.equal(out.errors.length, 1);
  assert.match(out.errors[0], /unknown allowed model "ghost"/);
});

test("demand: quoted fields with commas and quotes survive export → import", () => {
  const entries = demand.parseDemandCsv(
    'cluster,period,network\n"c,1",2026-01,"say ""hi"""\n', SPECS, MODELS).entries;
  assert.equal(entries[0].cluster_id, "c,1");
  assert.equal(entries[0].network, 'say "hi"');
  const again = demand.parseDemandCsv(demand.exportDemandCsv(entries, SPECS), SPECS, MODELS);
  assert.deepEqual(again.errors, []);
  assert.deepEqual(again.entries, entries);
});

test("splitAllowed accepts ; and | inside one cell", () => {
  assert.deepEqual(demand.splitAllowed("small; big|huge ;"), ["small", "big", "huge"]);
  assert.deepEqual(demand.splitAllowed(null), []);
});

test("headerish tells a header line from a data line", () => {
  assert.equal(demand.headerish("fab,cluster", ","), true);
  assert.equal(demand.headerish("fab-a,c1,worker", ","), false);
});

test("in-stock sample imports clean and exports byte-identical", () => {
  const text = readText("capacity/in_stock_sample.csv");
  const out = stock.analyzeStockCsv(text, MODELS);
  assert.deepEqual(out.globalErrors, []);
  assert.deepEqual(out.rows.flatMap((r) => r.errors), []);
  assert.equal(out.summary.machines, out.groups.reduce((n, g) => n + g.count, 0));
  assert.equal(stock.exportStockCsv(out.groups, MODELS), text);
});

test("in-stock: count, catalog and duplicate errors land on their rows", () => {
  const out = stock.analyzeStockCsv(
    "model,count\nstd-64c,0\nzzz,2\nstd-64c,3\nstd-64c,3\n", MODELS);
  assert.deepEqual(out.groups, []);
  const errs = out.rows.map((r) => r.errors);
  assert.match(errs[0][0], /count must be a positive integer/);
  assert.match(errs[1][0], /unknown model "zzz"/);
  assert.deepEqual(errs[2], []);
  assert.match(errs[3][0], /duplicate/);
});

test("validateStock flags duplicates and non-positive counts in form state", () => {
  const g = { fab: "f", dc: "d", ag: "a", network: "n", modelIdx: 0, count: 2 };
  const { rowErrors } = stock.validateStock([g, { ...g }, { ...g, ag: "b", count: 0 }], MODELS);
  assert.deepEqual(rowErrors[0], []);
  assert.match(rowErrors[1][0], /duplicate of row 1/);
  assert.match(rowErrors[2][0], /count must be a positive integer/);
});
