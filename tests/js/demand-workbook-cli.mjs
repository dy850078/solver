// Bridge for tests/test_ui_js.py — NOT a test file (no .test. in the name).
//
//   node demand-workbook-cli.mjs <request.json> <out.xlsx> <out.json>
//
// Writes the PM Demand workbook the UI would download for <request.json>
// (xlsx.js, the hand-written ZIP writer) and the request the UI would get
// back from importing those sheets into an empty form (demand-xlsx.js).
// pytest then judges both with tools that share no code with the UI:
// Python's zipfile/ElementTree for the bytes, the Pydantic model for the
// request.
import { readFileSync, writeFileSync } from "node:fs";
import { JS } from "./helpers.mjs";

const { requestToSheets, sheetsToRequest } = await import(JS + "demand-xlsx.js");
const { buildXlsx } = await import(JS + "xlsx.js");

const [src, outXlsx, outJson] = process.argv.slice(2);
const request = JSON.parse(readFileSync(src, "utf8"));
const sheets = requestToSheets(request);

writeFileSync(outXlsx, Buffer.from(await buildXlsx(sheets).arrayBuffer()));

const imported = sheetsToRequest(sheets, {});
if (imported.errorCount) {
  console.error(JSON.stringify(imported.errors));
  process.exit(1);
}
writeFileSync(outJson, JSON.stringify(imported.request));
