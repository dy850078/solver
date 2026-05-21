/**
 * Lightweight multi-select dropdown.
 *
 * Renders a trigger button + a popover panel of checkboxes. Built with
 * createElement + textContent only (no innerHTML), so it's safe even if
 * option labels come from user input.
 *
 * Usage:
 *   const ms = createMultiSelect({
 *     label: "Cluster",
 *     options: [{ value: "cluster-A", count: 12 }, ...],
 *     selected: new Set(),
 *     onChange: (selectedSet) => { ... },
 *   });
 *   container.appendChild(ms.element);
 *   ms.update({ options, selected });   // re-render after data change
 */

let openInstance = null;

function closeOpen() {
  if (openInstance) {
    openInstance._setOpen(false);
    openInstance = null;
  }
}

document.addEventListener("click", (e) => {
  if (openInstance && !openInstance.element.contains(e.target)) closeOpen();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeOpen();
});

export function createMultiSelect({ label, options = [], selected = new Set(), onChange = () => {} } = {}) {
  const root = document.createElement("div");
  root.className = "ms";

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "ms__trigger";
  const labelEl = document.createElement("span");
  labelEl.className = "ms__label";
  labelEl.textContent = label;
  const valueEl = document.createElement("span");
  valueEl.className = "ms__value";
  const chevron = document.createElement("span");
  chevron.className = "ms__chevron";
  chevron.setAttribute("aria-hidden", "true");
  chevron.textContent = "▾";
  trigger.append(labelEl, valueEl, chevron);

  const panel = document.createElement("div");
  panel.className = "ms__panel";
  panel.hidden = true;
  panel.setAttribute("role", "listbox");

  root.append(trigger, panel);

  const state = { options, selected, label };

  function summaryText() {
    if (state.selected.size === 0) return "All";
    if (state.selected.size === 1) return [...state.selected][0];
    return `${state.selected.size} selected`;
  }

  function setOpen(open) {
    panel.hidden = !open;
    root.classList.toggle("is-open", open);
    trigger.setAttribute("aria-expanded", String(open));
    if (open) {
      openInstance = api;
    } else if (openInstance === api) {
      openInstance = null;
    }
  }

  function renderPanel() {
    panel.replaceChildren();

    if (state.options.length === 0) {
      const empty = document.createElement("div");
      empty.className = "ms__empty";
      empty.textContent = "No options available.";
      panel.appendChild(empty);
      return;
    }

    const actions = document.createElement("div");
    actions.className = "ms__actions";
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "ms__action";
    clear.textContent = "Clear";
    clear.addEventListener("click", (e) => {
      e.stopPropagation();
      state.selected.clear();
      renderPanel();
      renderTriggerValue();
      onChange(new Set(state.selected));
    });
    actions.appendChild(clear);
    panel.appendChild(actions);

    for (const opt of state.options) {
      const row = document.createElement("label");
      row.className = "ms__option";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.selected.has(opt.value);
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(opt.value);
        else state.selected.delete(opt.value);
        renderTriggerValue();
        onChange(new Set(state.selected));
      });
      const name = document.createElement("span");
      name.className = "ms__option-name";
      name.textContent = opt.value;
      const count = document.createElement("span");
      count.className = "ms__option-count";
      count.textContent = String(opt.count ?? 0);
      row.append(cb, name, count);
      panel.appendChild(row);
    }
  }

  function renderTriggerValue() {
    valueEl.textContent = summaryText();
    root.classList.toggle("is-empty", state.selected.size === 0);
  }

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (root.classList.contains("is-open")) {
      setOpen(false);
    } else {
      closeOpen();
      setOpen(true);
    }
  });

  const api = {
    element: root,
    _setOpen: setOpen,
    update({ options, selected }) {
      if (options !== undefined) state.options = options;
      if (selected !== undefined) state.selected = selected;
      renderPanel();
      renderTriggerValue();
    },
    getSelected() { return new Set(state.selected); },
    clear() {
      state.selected.clear();
      renderPanel();
      renderTriggerValue();
    },
  };

  renderPanel();
  renderTriggerValue();
  return api;
}
