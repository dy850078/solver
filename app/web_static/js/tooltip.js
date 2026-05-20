/**
 * Shared floating tooltip used by rack diagram and matrix views.
 * Anchored to #treemap-tooltip element in the DOM.
 *
 * Design: showTooltip only accepts DOM Nodes (typically a DocumentFragment
 * built by buildTooltipBody). innerHTML is never used here, so callers
 * cannot inject HTML into the tooltip even if their content is attacker-
 * controlled — textContent is the only sink for user data.
 */

export { escapeHtml } from "./util.js";

let tooltipEl = null;

function el() {
  if (!tooltipEl) tooltipEl = document.getElementById("treemap-tooltip");
  return tooltipEl;
}

function position(evt) {
  const t = el();
  if (!t) return;
  const pad = 12;
  const r = t.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + r.width + 8 > window.innerWidth) x = evt.clientX - r.width - pad;
  if (y + r.height + 8 > window.innerHeight) y = evt.clientY - r.height - pad;
  t.style.left = `${Math.max(8, x)}px`;
  t.style.top = `${Math.max(8, y)}px`;
}

/**
 * Show the tooltip with the given DOM content.
 * @param {Node} content - a DOM Node (typically a DocumentFragment).
 *                         String inputs are rejected to prevent HTML injection.
 */
export function showTooltip(content, evt) {
  const t = el();
  if (!t) return;
  if (!(content instanceof Node)) {
    // Defensive: refuse string content so we never re-introduce innerHTML.
    return;
  }
  t.replaceChildren(content);
  position(evt);
  t.classList.add("is-visible");
}

export function moveTooltip(evt) {
  if (el()?.classList.contains("is-visible")) position(evt);
}

export function hideTooltip() {
  el()?.classList.remove("is-visible");
}

/**
 * Build a standard tooltip body as a DocumentFragment.
 * All text is inserted via textContent, so user data cannot inject HTML.
 *
 * @param {string} title - the title line.
 * @param {Array<[string, string]>} rows - [key, value] tuples.
 * @returns {DocumentFragment}
 */
export function buildTooltipBody(title, rows) {
  const frag = document.createDocumentFragment();

  const titleEl = document.createElement("div");
  titleEl.className = "tooltip__title";
  titleEl.textContent = title ?? "";
  frag.appendChild(titleEl);

  for (const [k, v] of rows ?? []) {
    const rowEl = document.createElement("div");
    rowEl.className = "tooltip__row";

    const kEl = document.createElement("span");
    kEl.className = "k";
    kEl.textContent = k ?? "";
    rowEl.appendChild(kEl);

    const vEl = document.createElement("span");
    vEl.className = "v";
    vEl.textContent = v ?? "";
    rowEl.appendChild(vEl);

    frag.appendChild(rowEl);
  }
  return frag;
}
