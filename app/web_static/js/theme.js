/* Theme toggle. The current theme is applied before first paint by an inline
 * <head> script (localStorage "solver-theme", else the system preference);
 * this module only wires the header button. */

const KEY = "solver-theme";

document.getElementById("theme-toggle")?.addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem(KEY, next); } catch { /* private mode etc. */ }
});
