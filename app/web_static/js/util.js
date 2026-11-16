/**
 * Shared utilities for the web UI.
 *
 * escapeHtml is the single source of truth for HTML escaping. It escapes
 * the five characters needed for *both* element content and attribute
 * values, so any user-controlled string can be safely interpolated into:
 *   - `<tag>${escapeHtml(v)}</tag>`           (element content)
 *   - `<tag attr="${escapeHtml(v)}">`         (double-quoted attribute)
 *   - `<tag attr='${escapeHtml(v)}'>`         (single-quoted attribute)
 *
 * Do NOT use for CSS values, JS string literals, or URL contexts — those
 * need different escaping rules.
 */

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

/**
 * Per-GPU-model accounting helpers (`Resources.gpu`, e.g. {"h200": 4}).
 * The wire contract removed the scalar gpu_count — forms must emit the
 * per-model dict, and displays should render "4×h200" style strings.
 *
 * parseGpu:  "h200:4, a100:2" → {h200: 4, a100: 2}  (blank/invalid → {})
 * formatGpu: {h200: 4}        → "h200:4"            (sorted, stable)
 */
export function parseGpu(text) {
  const out = {};
  String(text || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .forEach((t) => {
      const idx = t.lastIndexOf(":");
      if (idx <= 0) return;
      const model = t.slice(0, idx).trim();
      const count = Number(t.slice(idx + 1));
      if (model && Number.isFinite(count) && count > 0) out[model] = Math.round(count);
    });
  return out;
}

export function formatGpu(gpu) {
  return Object.entries(gpu || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([m, c]) => `${m}:${c}`)
    .join(",");
}

/* VM labels (ADR-021) travel as {key: value}; the form edits them as free
 * text, one `key=value` per comma. An empty value is legal ("dedicated=")
 * and means "the group without this label" to selectors, so it is kept.
 * The server validates the charset (^[\w.-]+$ parts); the parser only
 * splits — anything without "=" or with an empty key is dropped.
 *
 * parseLabels:  "dedicated=AAA, tier=gold" → {dedicated: "AAA", tier: "gold"}
 * formatLabels: {tier: "gold", dedicated: "AAA"} → "dedicated=AAA, tier=gold"
 */
export function parseLabels(text) {
  const out = {};
  String(text || "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .forEach((t) => {
      const idx = t.indexOf("=");
      if (idx <= 0) return;
      const key = t.slice(0, idx).trim();
      if (key) out[key] = t.slice(idx + 1).trim();
    });
  return out;
}

export function formatLabels(labels, sep = ", ") {
  return Object.entries(labels || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join(sep);
}

/** Human-readable GPU summary for displays: {h200:4, a100:2} → "4×h200 + 2×a100". */
export function gpuSummary(gpu) {
  const parts = Object.entries(gpu || {}).map(([m, c]) => `${c}×${m}`);
  return parts.length ? parts.join(" + ") : "0";
}
