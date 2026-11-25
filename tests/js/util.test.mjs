import test from "node:test";
import assert from "node:assert/strict";
import { JS } from "./helpers.mjs";

const { escapeHtml, parseGpu, formatGpu, parseLabels, formatLabels, demandIdentity, gpuSummary } =
  await import(JS + "util.js");

test("escapeHtml neutralises every character that can open markup", () => {
  assert.equal(escapeHtml(`<img src=x onerror="a('b')">&`),
    "&lt;img src=x onerror=&quot;a(&#39;b&#39;)&quot;&gt;&amp;");
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(0), "0");
});

test("parseGpu keeps positive per-model counts and drops junk", () => {
  assert.deepEqual(parseGpu("h200:4, a100:2"), { h200: 4, a100: 2 });
  assert.deepEqual(parseGpu(""), {});
  assert.deepEqual(parseGpu("h200, :3, a100:0, t4:-1, v100:x"), {});
  // model names may contain ':' — the LAST colon separates the count
  assert.deepEqual(parseGpu("nvidia:h200:8"), { "nvidia:h200": 8 });
});

test("formatGpu is sorted and round-trips through parseGpu", () => {
  const gpu = { h200: 4, a100: 2 };
  assert.equal(formatGpu(gpu), "a100:2,h200:4");
  assert.deepEqual(parseGpu(formatGpu(gpu)), gpu);
  assert.equal(formatGpu(undefined), "");
});

test("labels: an empty value is legal and survives the round trip", () => {
  assert.deepEqual(parseLabels("dedicated=AAA, tier=gold"), { dedicated: "AAA", tier: "gold" });
  assert.deepEqual(parseLabels("dedicated="), { dedicated: "" });
  assert.deepEqual(parseLabels("novalue, =x"), {});
  const labels = { tier: "gold", dedicated: "" };
  assert.equal(formatLabels(labels), "dedicated=, tier=gold");
  assert.deepEqual(parseLabels(formatLabels(labels)), labels);
});

test("demandIdentity: role defaults to worker, labels are part of identity", () => {
  const base = { cluster_id: "A1", ip_type: "routable" };
  assert.equal(demandIdentity({ ...base }), demandIdentity({ ...base, node_role: "worker" }));
  assert.equal(demandIdentity({ ...base, cluster_id: " A1 " }), demandIdentity(base));
  assert.notEqual(demandIdentity({ ...base, labels: { dedicated: "AAA" } }), demandIdentity(base));
  assert.notEqual(demandIdentity({ ...base, labels: { dedicated: "AAA" } }),
    demandIdentity({ ...base, labels: { dedicated: "BBB" } }));
  // key order must not matter
  assert.equal(demandIdentity({ ...base, labels: { a: "1", b: "2" } }),
    demandIdentity({ ...base, labels: { b: "2", a: "1" } }));
});

test("gpuSummary", () => {
  assert.equal(gpuSummary({ h200: 4, a100: 2 }), "4×h200 + 2×a100");
  assert.equal(gpuSummary({}), "0");
});
