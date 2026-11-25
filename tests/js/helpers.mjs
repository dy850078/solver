// Shared paths for the UI unit tests. Run with:  node --test tests/js/
// No npm dependencies on purpose — node's built-in runner and assert only,
// so the suite works on a closed network and adds no third-party JS.
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

export const ROOT = fileURLToPath(new URL("../../", import.meta.url));
export const JS = new URL("../../app/web_static/js/", import.meta.url).href;
export const EXAMPLES = ROOT + "examples/";

export const readText = (rel) => readFileSync(EXAMPLES + rel, "utf8");
export const readJson = (rel) => JSON.parse(readText(rel));
export const listExamples = (dir, ext) =>
  readdirSync(EXAMPLES + dir).filter((f) => f.endsWith(ext)).sort();

/** Drop empty arrays/objects recursively: the importer spells "absent" as
 *  [] where a hand-written template simply omits the key. */
export function prune(v) {
  if (Array.isArray(v)) return v.map(prune);
  if (v && typeof v === "object") {
    const out = {};
    for (const [k, x] of Object.entries(v)) {
      const p = prune(x);
      const empty = Array.isArray(p) ? p.length === 0
        : p && typeof p === "object" && Object.keys(p).length === 0;
      if (!empty) out[k] = p;
    }
    return out;
  }
  return v;
}
