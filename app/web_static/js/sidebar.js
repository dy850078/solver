/* Sidebar chrome shared by every page with a `.sidebar` input panel.
 *
 * A drag handle on the divider resizes the panel (persisted in localStorage
 * "sidebarW", so the width follows the user across pages) and a chevron tab
 * ON that divider collapses it. Collapsed, the tab docks to the left screen
 * edge — collapse and expand always live on the same line. CSS lives in
 * styles.css (#sidebar-resizer, #sidebar-collapse-tab, body.sidebar-collapsed).
 * Pages without a `.sidebar` return early.
 */

// Sidebar chrome: a drag handle on the divider to resize (persists), and a
// chevron tab ON that divider to collapse. Collapsed, the tab docks to the
// left screen edge — collapse and expand always live on the same line, so
// the control never has to be hunted down elsewhere in the UI.
export function initSidebarChrome() {
  const sidebar = document.querySelector(".sidebar");
  if (!sidebar) return;
  const root = document.documentElement;
  const MIN = 300, MAX = 760;

  const saved = parseInt(localStorage.getItem("sidebarW") || "", 10);
  if (saved >= MIN && saved <= MAX) root.style.setProperty("--sidebar-w", saved + "px");

  const handle = document.createElement("div");
  handle.id = "sidebar-resizer";
  document.body.appendChild(handle);

  const tab = document.createElement("button");
  tab.id = "sidebar-collapse-tab";
  tab.type = "button";
  tab.innerHTML = `<svg viewBox="0 0 8 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 1.5 2 6l3.5 4.5"/></svg>`;
  document.body.appendChild(tab);

  const collapsed = () => document.body.classList.contains("sidebar-collapsed");
  const place = () => {
    if (collapsed()) {
      tab.style.left = "0px";
    } else {
      const right = sidebar.getBoundingClientRect().right;
      handle.style.left = right + "px";
      tab.style.left = (right - 10) + "px";
    }
  };
  const setCollapsed = (on) => {
    document.body.classList.toggle("sidebar-collapsed", on);
    tab.title = on ? "Show input panel" : "Hide input panel";
    tab.setAttribute("aria-expanded", String(!on));
    localStorage.setItem("solver-sidebar-collapsed", on ? "1" : "0");
    place();
  };
  setCollapsed(localStorage.getItem("solver-sidebar-collapsed") === "1");
  window.addEventListener("resize", place);

  tab.addEventListener("click", () => setCollapsed(!collapsed()));
  handle.addEventListener("dblclick", () => setCollapsed(true));

  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    handle.classList.add("dragging");
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    const startX = e.clientX;
    const startW = sidebar.getBoundingClientRect().width;
    const onMove = (ev) => {
      const w = Math.min(MAX, Math.max(MIN, Math.round(startW + ev.clientX - startX)));
      root.style.setProperty("--sidebar-w", w + "px");
      place();
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      handle.classList.remove("dragging");
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      const cur = parseInt(getComputedStyle(root).getPropertyValue("--sidebar-w"), 10);
      if (cur) localStorage.setItem("sidebarW", String(cur));
      place();
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
