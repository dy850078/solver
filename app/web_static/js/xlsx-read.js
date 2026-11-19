/* Minimal dependency-free .xlsx READER — the mirror of xlsx.js (the writer).
 *
 * The UI runs with no third-party JS and no CDN, so instead of SheetJS we
 * lean on two things every current browser already ships:
 *
 *   1. An .xlsx file is a plain ZIP archive holding a handful of XML parts
 *      (xl/workbook.xml, xl/sharedStrings.xml, xl/worksheets/sheetN.xml …).
 *      ZIP entries are either stored (method 0, bytes as-is) or deflated
 *      (method 8); the browser's DecompressionStream("deflate-raw") inflates
 *      the latter natively (Chrome/Edge 103+, Firefox 113+, Safari 16.4+).
 *   2. DOMParser parses the XML parts; matching elements by localName keeps
 *      us independent of namespace prefixes (Excel "Strict Open XML" uses
 *      different namespace URIs for the same element names).
 *
 * readXlsx(arrayBuffer) → [{ name, rows, hidden, errorCells }]
 *   rows: dense 2-D array; a cell is string | number | boolean | null.
 *   Shared strings (incl. rich-text runs, phonetic <rPh> skipped), inline
 *   strings, booleans, formula cells (cached value) and error cells (→ null)
 *   are handled; dates stay as their raw serial number (no date columns in
 *   any schema this page reads). Not handled on purpose: ZIP64, deflate64,
 *   encrypted workbooks (an OLE container, reported as such), UTF-16 XML.
 *
 * ZIP layout reminder (all little-endian): local header + data per entry,
 * then a central directory describing every entry, then the end-of-central-
 * directory record (EOCD). We trust the CENTRAL directory for method, sizes
 * and offsets — the local header may carry zeros when the writer streamed
 * the file (data-descriptor flag) — and read the local header only for its
 * own name/extra lengths to locate the data start.
 */

const SIG_EOCD = 0x06054b50, SIG_CENTRAL = 0x02014b50, SIG_LOCAL = 0x04034b50;

function readZipDirectory(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const n = bytes.length;
  let eocd = -1;
  for (let i = n - 22; i >= Math.max(0, n - 22 - 0xFFFF); i--) {
    if (dv.getUint32(i, true) === SIG_EOCD) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("not a .xlsx file (no ZIP directory found)");
  const count = dv.getUint16(eocd + 10, true);
  const cdOffset = dv.getUint32(eocd + 16, true);
  if (count === 0xFFFF || cdOffset === 0xFFFFFFFF) throw new Error("ZIP64 archives are not supported");
  const entries = new Map();
  const dec = new TextDecoder();
  let p = cdOffset;
  for (let i = 0; i < count; i++) {
    if (p + 46 > n || dv.getUint32(p, true) !== SIG_CENTRAL) throw new Error("corrupt ZIP central directory");
    const flags = dv.getUint16(p + 8, true);
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const usize = dv.getUint32(p + 24, true);
    const nameLen = dv.getUint16(p + 28, true);
    const extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    const localOffset = dv.getUint32(p + 42, true);
    const name = dec.decode(bytes.subarray(p + 46, p + 46 + nameLen));
    entries.set(name.toLowerCase(), { name, flags, method, csize, usize, localOffset });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

async function inflateRaw(slice) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("this browser cannot decompress .xlsx files (no DecompressionStream) — "
      + "use a current Chrome / Edge / Firefox / Safari, or load a .json template instead");
  }
  const stream = new Blob([slice]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

async function entryBytes(bytes, entry) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const p = entry.localOffset;
  if (p + 30 > bytes.length || dv.getUint32(p, true) !== SIG_LOCAL) throw new Error(`corrupt ZIP entry ${entry.name}`);
  if (entry.flags & 0x1) throw new Error("the workbook is password-protected — remove the password and save again");
  const start = p + 30 + dv.getUint16(p + 26, true) + dv.getUint16(p + 28, true);
  const data = bytes.subarray(start, start + entry.csize);
  if (entry.method === 0) return data;
  if (entry.method === 8) return entry.csize === 0 ? new Uint8Array(0) : inflateRaw(data);
  throw new Error(`unsupported ZIP compression method ${entry.method} in ${entry.name}`);
}

/* ── XML helpers (namespace-agnostic) ─────────────────────────────── */

function parseXml(text, what) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.getElementsByTagName("parsererror").length) throw new Error(`malformed XML in ${what}`);
  return doc;
}
const byLocal = (el, name) => [...el.getElementsByTagNameNS("*", name)];
const attrLocal = (el, name) => {
  for (const a of el.attributes) if (a.localName === name) return a.value;
  return null;
};

/** Text of every <t> under `el`, skipping phonetic guides (<rPh>). */
function richText(el) {
  let out = "";
  for (const t of byLocal(el, "t")) {
    let p = t.parentNode, phonetic = false;
    while (p && p !== el) { if (p.localName === "rPh") { phonetic = true; break; } p = p.parentNode; }
    if (!phonetic) out += t.textContent;
  }
  return out;
}

/** "AB" → 27 (0-based 27? no: A=0, B=1 … AA=26). Returns 0-based index. */
function colIndex(ref) {
  let n = 0;
  for (const ch of ref) {
    const c = ch.charCodeAt(0);
    if (c < 65 || c > 90) break;
    n = n * 26 + (c - 64);
  }
  return n - 1;
}

function normalizePartName(target, baseDir) {
  let t = target.replace(/\\/g, "/");
  if (t.startsWith("/")) t = t.slice(1);
  else t = baseDir + t;
  const parts = [];
  for (const seg of t.split("/")) {
    if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.join("/").toLowerCase();
}

/* ── the reader ───────────────────────────────────────────────────── */

export async function readXlsx(arrayBuffer) {
  const bytes = new Uint8Array(arrayBuffer);
  if (bytes.length >= 4 && bytes[0] === 0xD0 && bytes[1] === 0xCF && bytes[2] === 0x11 && bytes[3] === 0xE0) {
    throw new Error("this is an OLE file — an encrypted workbook or a legacy .xls; save it as .xlsx (unencrypted) and try again");
  }
  const entries = readZipDirectory(bytes);
  const dec = new TextDecoder();
  const text = async (part) => {
    const e = entries.get(part.toLowerCase());
    return e ? dec.decode(await entryBytes(bytes, e)) : null;
  };

  const wbXml = await text("xl/workbook.xml");
  if (wbXml == null) throw new Error("not a workbook (xl/workbook.xml missing)");
  const wb = parseXml(wbXml, "xl/workbook.xml");

  // Relationships: rId → part name; sheets without a usable rel fall back
  // to xl/worksheets/sheetN.xml by position.
  const rels = new Map();
  const relXml = await text("xl/_rels/workbook.xml.rels");
  if (relXml != null) {
    for (const r of byLocal(parseXml(relXml, "workbook.xml.rels"), "Relationship")) {
      const id = r.getAttribute("Id"), type = r.getAttribute("Type") || "", target = r.getAttribute("Target") || "";
      if (!id || !target) continue;
      if (type.endsWith("/worksheet")) rels.set(id, normalizePartName(target, "xl/"));
    }
  }

  const shared = [];
  const ssXml = await text("xl/sharedStrings.xml");
  if (ssXml != null) {
    for (const si of byLocal(parseXml(ssXml, "sharedStrings.xml"), "si")) shared.push(richText(si));
  }

  const sheets = [];
  const sheetEls = byLocal(wb, "sheet");
  for (let i = 0; i < sheetEls.length; i++) {
    const el = sheetEls[i];
    const name = el.getAttribute("name") || `Sheet${i + 1}`;
    const rid = attrLocal(el, "id");
    const part = (rid && rels.get(rid)) || `xl/worksheets/sheet${i + 1}.xml`;
    const xml = await text(part);
    if (xml == null) continue;   // chartsheet or missing part: nothing tabular to read
    const hidden = (el.getAttribute("state") || "visible") !== "visible";
    sheets.push({ name, hidden, ...readSheet(parseXml(xml, part), shared) });
  }
  return sheets;
}

function readSheet(doc, shared) {
  const rows = [];
  let errorCells = 0;
  let rowCursor = 0;
  for (const rowEl of byLocal(doc, "row")) {
    const rAttr = Number(rowEl.getAttribute("r"));
    const ri = Number.isFinite(rAttr) && rAttr > 0 ? rAttr - 1 : rowCursor;
    rowCursor = ri + 1;
    const row = rows[ri] || (rows[ri] = []);
    let colCursor = 0;
    for (const c of rowEl.children) {
      if (c.localName !== "c") continue;
      const ref = c.getAttribute("r");
      const ci = ref ? colIndex(ref) : colCursor;
      colCursor = ci + 1;
      const t = c.getAttribute("t") || "n";
      const vEl = [...c.children].find((x) => x.localName === "v");
      const v = vEl ? vEl.textContent : null;
      let value = null;
      if (t === "s") value = v != null && v !== "" ? (shared[Number(v)] ?? null) : null;
      else if (t === "inlineStr") { const is = [...c.children].find((x) => x.localName === "is"); value = is ? richText(is) : null; }
      else if (t === "b") value = v === "1" || v === "true";
      else if (t === "str" || t === "d") value = v;
      else if (t === "e") { value = null; errorCells++; }
      else value = v == null || v === "" ? null : (Number.isFinite(Number(v)) ? Number(v) : v);
      row[ci] = value;
    }
  }
  // Dense: fill holes with null, pad every row to the same width, drop
  // trailing all-empty rows.
  const width = rows.reduce((m, r) => Math.max(m, r ? r.length : 0), 0);
  const dense = [];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i] || [];
    const out = new Array(width).fill(null);
    for (let j = 0; j < r.length; j++) out[j] = r[j] === undefined ? null : r[j];
    dense.push(out);
  }
  while (dense.length && dense[dense.length - 1].every((c) => c == null || c === "")) dense.pop();
  return { rows: dense, errorCells };
}
