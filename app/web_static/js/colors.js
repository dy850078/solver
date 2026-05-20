// Modern Tailwind-inspired palette, harmonious across hues, color-blind friendly.
const PALETTE_PRIMARY = [
  "#6366f1", // indigo
  "#10b981", // emerald
  "#f59e0b", // amber
  "#ec4899", // pink
  "#06b6d4", // cyan
  "#8b5cf6", // violet
  "#84cc16", // lime
  "#f43f5e", // rose
  "#0ea5e9", // sky
  "#a855f7", // purple
];

const PALETTE_OVERFLOW = [
  ...PALETTE_PRIMARY,
  "#14b8a6", // teal
  "#eab308", // yellow
  "#ef4444", // red
  "#22c55e", // green
  "#3b82f6", // blue
  "#d946ef", // fuchsia
];

const MUTED = "#cbd5e1";

let agOrder = [];
let agToColor = new Map();

export function rebuildColorScale(agSet) {
  agOrder = Array.from(agSet).sort();
  const palette = agOrder.length > PALETTE_PRIMARY.length ? PALETTE_OVERFLOW : PALETTE_PRIMARY;
  agToColor = new Map(agOrder.map((ag, i) => [ag, palette[i % palette.length]]));
}

export function colorForAg(ag) {
  if (!ag) return MUTED;
  return agToColor.get(ag) ?? MUTED;
}

export function legendEntries() {
  return agOrder.map((ag) => ({ ag, color: colorForAg(ag) }));
}
