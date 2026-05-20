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
