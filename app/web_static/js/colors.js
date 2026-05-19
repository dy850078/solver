import * as d3 from "d3";

const PALETTE_10 = d3.schemeTableau10;
const PALETTE_12 = d3.schemeSet3;
const MUTED = "#cbd5e1";

let scale = null;
let agOrder = [];

export function rebuildColorScale(agSet) {
  agOrder = Array.from(agSet).sort();
  const palette = agOrder.length > 10 ? PALETTE_12 : PALETTE_10;
  scale = d3.scaleOrdinal().domain(agOrder).range(palette);
}

export function colorForAg(ag) {
  if (!ag) return MUTED;
  if (!scale) return MUTED;
  return scale(ag);
}

export function legendEntries() {
  return agOrder.map((ag) => ({ ag, color: colorForAg(ag) }));
}
