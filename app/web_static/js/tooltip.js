/**
 * Shared floating tooltip used by rack diagram and matrix views.
 * Anchored to #treemap-tooltip element in the DOM.
 */

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

export function showTooltip(html, evt) {
  const t = el();
  if (!t) return;
  t.innerHTML = html;
  position(evt);
  t.classList.add("is-visible");
}

export function moveTooltip(evt) {
  if (el()?.classList.contains("is-visible")) position(evt);
}

export function hideTooltip() {
  el()?.classList.remove("is-visible");
}

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
