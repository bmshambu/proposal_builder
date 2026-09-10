/* The author console.
 *
 * Every number on screen comes from the engine. There is no client-side model
 * of what a deck contains: which slides an answer set produces is decided by
 * rules.py through /select, the same code that builds the file, so the preview
 * cannot drift from the result.
 *
 * Thumbnails are real slides rendered by engine/svg.py, not drawings of
 * slides. That is the difference between browsing a library and looking at a
 * diagram of one.
 */
"use strict";

// --------------------------------------------------------------- plumbing
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/** FastAPI puts the message in `detail`, and a whole list there for a
 *  validation failure. Flatten both, or the UI shows "[object Object]" at
 *  exactly the moment someone needs to know what went wrong. */
function problem(body, fallback) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map(e => e.msg || JSON.stringify(e)).join("; ");
  return fallback || "something went wrong";
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(problem(body, r.statusText));
  return body;
}

const getJSON = (p) => api(p);
const sendJSON = (p, obj, method) => api(p, {
  method: method || "POST",
  // A DELETE carries no body, and sending "null" as one makes FastAPI reject
  // the request for a malformed payload it never wanted.
  ...(obj === null ? {} : { headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(obj) }),
});

let flashTimer = null;
function flash(message, kind) {
  const el = $("flash");
  el.textContent = message;
  el.className = "flash " + (kind || "ok");
  el.hidden = false;
  clearTimeout(flashTimer);
  // Errors stay until dismissed by the next action; successes fade. An error
  // that disappears while you are reading it teaches people to distrust the
  // whole strip.
  if (kind !== "bad") flashTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

/** A file input that is never in the markup - dropzones look better and both
 *  paths end in the same handler. */
function filePicker(accept, onFile) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = accept;
  input.hidden = true;
  input.onchange = () => { if (input.files[0]) onFile(input.files[0]); input.value = ""; };
  document.body.appendChild(input);
  return input;
}

function dropTarget(el, accept, onFile) {
  ["dragenter", "dragover"].forEach(ev => el.addEventListener(ev, e => {
    e.preventDefault(); el.classList.add("over");
  }));
  ["dragleave", "drop"].forEach(ev => el.addEventListener(ev, e => {
    e.preventDefault(); el.classList.remove("over");
  }));
  el.addEventListener("drop", e => {
    const f = e.dataTransfer.files[0];
    if (!f) return;
    if (accept && !f.name.toLowerCase().endsWith(accept)) {
      flash(`${f.name} is not a ${accept} file`, "bad");
      return;
    }
    onFile(f);
  });
}

// ------------------------------------------------------------------ state
const S = {
  libs: [], lib: null,
  inspect: null, thumbs: {},          // block id -> svg string
  deck: [], bindings: {},
  fields: {}, presets: [],
  answers: {},
  dirty: false, editing: null, built: null,
  check: { ours: null, templafy: null },
  slides: [], slideAt: 0,        // the built deck, for the viewer
};

const block = (id) => (S.inspect ? S.inspect.blocks.find(b => b.id === id) : null);
const titleOf = (id) => (block(id) || {}).title || id;
const label = (field) => String(field).replace(/_/g, " ").trim();

// ------------------------------------------------------------------ dates
// Payloads store YYYYMMDD; <input type=date> speaks YYYY-MM-DD only. Convert at
// the edges so what is stored stays exactly what the payload holds - a picker
// that wrote 2026-11-30 because it looked friendlier would break every
// comparison in rules.py while appearing to work.
const isDate = (f) => (S.fields[f] || {}).kind === "date";
const toISO = (raw) => {
  const s = String(raw ?? "").replace(/\D/g, "");
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : "";
};
const fromISO = (iso) => String(iso || "").replace(/\D/g, "");
const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];
function humanDate(raw) {
  const s = String(raw ?? "").replace(/\D/g, "");
  if (s.length !== 8) return String(raw ?? "");
  const m = +s.slice(4, 6);
  return (m < 1 || m > 12) ? String(raw)
    : `${MONTHS[m - 1]} ${+s.slice(6, 8)}, ${s.slice(0, 4)}`;
}

/** Preview a date in a chosen format. Mirrors engine/bindings.date_formats(),
 *  so the sample on the Values screen is what the deck will actually say -
 *  a preview that renders a date differently from the builder is worse than
 *  no preview, because it is believed. */
function formatted(raw, fmt) {
  const s = String(raw ?? "").replace(/\D/g, "");
  if (s.length !== 8) return String(raw ?? "");
  const y = s.slice(0, 4), m = +s.slice(4, 6), d = +s.slice(6, 8);
  if (m < 1 || m > 12) return String(raw);
  const mon = MONTHS[m - 1], ab = mon.slice(0, 3);
  const p2 = (n) => String(n).padStart(2, "0");
  return ({
    long_comma: `${mon} ${d}, ${y}`,
    day_month: `${d} ${mon} ${y}`,
    abbr_comma: `${ab} ${d}, ${y}`,
    day_abbr: `${d} ${ab} ${y}`,
    us_slash: `${p2(m)}/${p2(d)}/${y}`,
    eu_slash: `${p2(d)}/${p2(m)}/${y}`,
    iso: `${y}-${p2(m)}-${p2(d)}`,
    raw: s,
  })[fmt] ?? s;
}

// ------------------------------------------------------------- thumbnails
/** engine/svg.py returns a viewBox-only <svg>, so it scales to whatever box we
 *  give it. Injecting the class beats wrapping it: the existing CSS already
 *  styles `.thumb`. */
function thumb(id, size) {
  const svg = S.thumbs[id];
  if (!svg) return `<span class="thumb ${size || ""} pending"></span>`;
  return svg.replace("<svg ", `<svg class="thumb ${size || ""}" `);
}

async function loadThumbs(ids) {
  const want = ids.filter(i => !(i in S.thumbs));
  if (!want.length) return;
  const rows = await getJSON(
    `/api/libraries/${S.lib}/thumbs?blocks=${encodeURIComponent(want.join(","))}`);
  rows.forEach(r => { S.thumbs[r.id] = r.svg || ""; });
}

// ------------------------------------------------------------------ theme
const THEMES = ["system", "light", "dark"];
let theme = "system";
try { theme = localStorage.getItem("pptgen3-theme") || "system"; } catch (_) { }
if (!THEMES.includes(theme)) theme = "system";
function applyTheme() {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  $("theme-label").textContent = theme[0].toUpperCase() + theme.slice(1);
  try { localStorage.setItem("pptgen3-theme", theme); } catch (_) { }
}
$("theme-btn").onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme();
};

// ------------------------------------------------------------------- tabs
function showTab(tab) {
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll(".screen").forEach(s =>
    s.classList.toggle("on", s.id === "screen-" + tab));
  // Check has no tab of its own any more, so the header button has to show
  // that you are on it - otherwise nothing on screen says where you are.
  document.querySelectorAll("[data-goto]").forEach(b =>
    b.classList.toggle("here", b.dataset.goto === tab));
  $("testbar").hidden = tab !== "rules";
  fitMain();
}
document.querySelectorAll(".tabs button").forEach(b =>
  b.onclick = () => showTab(b.dataset.tab));
document.querySelectorAll("[data-goto]").forEach(b =>
  b.onclick = () => showTab(b.dataset.goto));
function fitMain() {
  const bar = $("testbar");
  document.querySelector("main").style.paddingBottom =
    bar.hidden ? "30px" : (bar.offsetHeight + 24) + "px";
}
window.addEventListener("resize", fitMain);

// -------------------------------------------------------------- libraries
async function loadLibraries(select) {
  S.libs = await getJSON("/api/libraries");
  const pick = select || (S.libs.find(l => l.real) || S.libs[0] || {}).id || null;
  renderLibPicker();
  if (pick && pick !== S.lib) await openLibrary(pick);
  else if (!pick) emptyState();
}

function renderLibPicker() {
  $("library").innerHTML =
    S.libs.map(l => `<option value="${esc(l.id)}"${l.id === S.lib ? " selected" : ""}>
      ${esc(l.name || l.id)}</option>`).join("")
    + `<option value="__new">+ Add a library…</option>`;
  $("hdr-count").textContent = S.inspect
    ? `${S.inspect.blocks.length} slides` : "";
}

$("library").onchange = async function () {
  if (this.value === "__new") {
    renderLibPicker(); showTab("library");
    $("lib-name").value = ""; $("lib-desc").value = "";
    $("lib-name").dispatchEvent(new Event("input"));
    flash("Drop a .pptx to add a library", "ok");
    return;
  }
  await openLibrary(this.value);
};

function emptyState() {
  S.lib = null; S.inspect = null; S.deck = []; S.thumbs = {};
  document.body.classList.add("empty");
  showTab("library");
  $("drop-hint").textContent = "start here";
  $("current").innerHTML = "";
}

async function openLibrary(id) {
  S.lib = id;
  S.thumbs = {}; S.built = null; S.editing = null; S.dirty = false;
  document.body.classList.remove("empty");
  try {
    S.inspect = await getJSON(`/api/libraries/${id}`);
  } catch (err) { flash(err.message, "bad"); return; }

  const rules = await getJSON(`/api/libraries/${id}/rules`);
  S.deck = rules.deck;
  S.bindings = rules.placeholders || {};

  // Answer sets belong to the library they describe, so they are re-read on
  // every switch. Carrying the previous library's fields across would offer
  // conditions on questions this template never asks.
  try {
    const cat = await getJSON(`/api/libraries/${id}/fields`);
    S.fields = cat.fields; S.presets = cat.payloads; S.shared = cat.shared;
  } catch (_) { S.fields = {}; S.presets = []; S.shared = false; }
  fillPresets();

  const meta = S.libs.find(l => l.id === id) || {};
  $("lib-name").value = id;
  $("lib-desc").value = meta.description || "";
  $("lib-name").dispatchEvent(new Event("input"));

  renderLibPicker();
  await renderLibrary();
  renderQuestions();
  renderMapping();
  renderRules();
  renderBuild();
}

// --------------------------------------------------- 1. library + inspect
async function renderLibrary() {
  const i = S.inspect;
  if (!i) return;
  $("drop-hint").textContent = "already imported — drop a new file to replace it";
  $("current").innerHTML = `
    <span class="f">${esc(i.library.id)}/library.pptx</span>
    <span class="m">${i.blocks.length} slides · ${i.placeholders.length} placeholders</span>
    <span class="sp"></span>
    <button class="btn" id="reinspect">Inspect again</button>`;
  $("reinspect").onclick = () => openLibrary(S.lib);

  const by = i.by_source || {};
  const titled = by.title || 0, positional = by.position || 0;
  $("stats").innerHTML = `
    <div class="stat"><b>${i.blocks.length}</b><span>slides found</span></div>
    <div class="stat ok"><b>${by.marker || 0}</b><span>stable ids</span></div>
    <div class="stat ${titled ? "warn" : ""}"><b>${titled}</b><span>named by title</span></div>
    <div class="stat ${positional ? "warn" : ""}"><b>${positional}</b><span>by position</span></div>
    <div class="stat"><b>${i.placeholders.length}</b><span>placeholders</span></div>
    <div class="stat ${i.unbound.length ? "bad" : "ok"}"><b>${i.unbound.length}</b><span>unbound</span></div>`;

  const bad = $("lib-bad");
  bad.innerHTML = i.unbound.length
    ? `<b>${i.unbound.length} placeholder(s) are not bound</b>
       ${i.unbound.map(p => "{{" + esc(p) + "}}").join(", ")} will appear in the
       deck as literal text. Bind them on the Mapping screen.` : "";
  bad.hidden = !i.unbound.length;

  const warn = $("lib-warn");
  const notes = [];
  if (titled + positional)
    notes.push(`<b>${titled + positional} slide(s) have no stable id</b>
      They are named by title or position, so editing the deck in PowerPoint can
      rename the block and break any rule pointing at it. Add a
      <code>{{block:id}}</code> marker in the slide.`);
  (i.drift || []).forEach(d => notes.push(`<b>Drift</b> ${esc(d)}`));
  (i.problems || []).forEach(p => notes.push(`<b>Rules</b> ${esc(p)}`));
  warn.innerHTML = notes.join("<hr>");
  warn.hidden = !notes.length;

  const ok = $("lib-ok");
  ok.innerHTML = `<b>Nothing in the deck was modified</b>
    The file is copied verbatim; identity lives in a sidecar beside it, so a
    firm template imports untouched and can be replaced at any time.`;
  ok.hidden = false;

  $("lib-count").textContent = `(${i.blocks.length})`;
  $("lib-grid").innerHTML = i.blocks.map(b => `
    <div class="card"><span class="thumb lg pending" data-thumb="${esc(b.id)}"></span>
      <div class="t" title="${esc(b.title)}">${esc(b.title)}</div>
      <div class="id">${esc(b.id)}</div>
      <div class="tags">
        ${b.source !== "marker" ? `<span class="tag w">named by ${esc(b.source)}</span>` : ""}
        ${b.placeholders.map(p => `<span class="tag ph">${esc(p)}</span>`).join("")}
        ${!b.placeholders.length ? '<span class="tag">no placeholders</span>' : ""}
      </div></div>`).join("");
  renderLibPicker();

  // Fetch after the grid is up: 128 slides is a real render, and an author
  // should see the list and the warnings immediately rather than a blank page
  // while the last thumbnail finishes.
  await loadThumbs(i.blocks.map(b => b.id));
  document.querySelectorAll("[data-thumb]").forEach(el => {
    const svg = S.thumbs[el.dataset.thumb];
    if (svg) el.outerHTML = thumb(el.dataset.thumb, "lg");
  });
}

// ----------------------------------------------------- upload a new library
const libPicker = filePicker(".pptx", uploadLibrary);
$("choose").onclick = () => libPicker.click();
dropTarget($("dropzone"), ".pptx", uploadLibrary);

async function uploadLibrary(file) {
  const name = ($("lib-name").value || "").trim();
  if (!/^[a-z0-9_]{1,64}$/.test(name)) {
    flash("Give the library a name first — lower case, digits and underscores", "bad");
    $("lib-name").focus();
    return;
  }
  const exists = S.libs.some(l => l.id === name);
  if (exists && !confirm(
    `"${name}" already exists.\n\nReplacing it overwrites its library AND its `
    + `rules. This cannot be undone from here.\n\nReplace it?`)) return;

  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  form.append("description", $("lib-desc").value || "");
  form.append("overwrite", exists ? "true" : "false");
  flash(`Importing ${file.name}…`, "ok");
  try {
    const r = await api("/api/libraries", { method: "POST", body: form });
    flash(`Imported ${r.imported.slides} slides as "${name}"`, "ok");
    S.lib = null;
    await loadLibraries(name);
  } catch (err) { flash(err.message, "bad"); }
}

$("lib-name").oninput = function () {
  const v = this.value.trim();
  const note = $("name-note");
  const clash = S.libs.some(l => l.id === v && l.id !== S.lib);
  const bad = v && !/^[a-z0-9_]+$/.test(v);
  note.className = "fld-note" + ((clash || bad) ? " warn" : "");
  note.textContent = !v ? "Required — this becomes the folder name."
    : bad ? "Lower case, digits and underscores only — it becomes a folder name."
      : clash ? `"${v}" already exists. Uploading replaces it, including its rules.`
        : `Saves to templates/${v}/ — its own library.pptx, rules.json and bindings.`;
};

// ------------------------------------------------------------- 2. rules
function renderRules() { renderPool(); renderDeck(); renderTest(); }

function renderPool() {
  const q = ($("pool-search").value || "").toLowerCase();
  const inDeck = new Set(S.deck.map(r => r.id));
  const rest = (S.inspect ? S.inspect.blocks : [])
    .filter(b => !inDeck.has(b.id))
    .filter(b => !q || b.id.includes(q) || b.title.toLowerCase().includes(q));
  $("pool-count").textContent = `(${rest.length})`;
  $("pool").innerHTML = rest.map(b => `
    <div class="pool-item" draggable="true" data-id="${esc(b.id)}">
      ${thumb(b.id, "xs")}
      <div class="meta"><div class="t" title="${esc(b.title)}">${esc(b.title)}</div>
        <div class="id">${esc(b.id)}</div></div>
      <button class="add" title="Add to the end of the deck" data-add="${esc(b.id)}">+</button>
    </div>`).join("")
    || `<p style="color:var(--muted);font-size:13px">Every slide is in the deck.</p>`;
}

function condText(when) {
  if (!when) return { lab: "always", op: "", val: "" };
  const f = label(when.field);
  const show = (v) => isDate(when.field) ? humanDate(v) : JSON.stringify(v);
  if ("exists" in when) return { lab: f, op: "is", val: "answered" };
  if ("eq" in when) return { lab: f, op: "is", val: show(when.eq) };
  if ("ne" in when) return { lab: f, op: "is not", val: show(when.ne) };
  if ("in" in when) return { lab: f, op: "is one of", val: when.in.join(", ") };
  if ("not_in" in when) return { lab: f, op: "is not one of", val: when.not_in.join(", ") };
  return { lab: f, op: "", val: "" };
}
const condPlain = (w) => w ? `${w.field} ${condText(w).op} ${condText(w).val}` : "always";

/** The same comparison rules.py makes, so a greyed row here means an absent
 *  slide there. It is only a preview: /select is what decides the build. */
function holds(when) {
  if (!when) return true;
  const v = S.answers[when.field];
  const s = (x) => String(x ?? "").replace(/\s+/g, " ").trim().toLowerCase();
  if ("exists" in when) return (s(v) !== "") === !!when.exists;
  if ("eq" in when) return s(v) === s(when.eq);
  if ("ne" in when) return s(v) !== s(when.ne);
  if ("in" in when) return when.in.some(x => s(x) === s(v));
  if ("not_in" in when) return !when.not_in.some(x => s(x) === s(v));
  return true;
}

function renderDeck() {
  let n = 0, off = 0;
  $("deck").innerHTML = S.deck.map(row => {
    const on = holds(row.when);
    on ? n++ : off++;
    const c = condText(row.when);
    return `<div class="row ${on ? "" : "excluded"}" draggable="true" data-id="${esc(row.id)}">
      <span class="grip">&#8942;&#8942;</span>
      <span class="num">${on ? n : "–"}</span>
      ${thumb(row.id, "sm")}
      <div class="namecell"><div class="t" title="${esc(titleOf(row.id))}">${esc(titleOf(row.id))}</div>
        <div class="id">${esc(row.id)}</div></div>
      <button class="chip ${row.when ? "cond" : ""}" data-edit="${esc(row.id)}"
        title="${esc(condPlain(row.when))}">
        <span class="lab">${esc(c.lab)}</span>
        ${c.op ? `<span class="op">${esc(c.op)}</span>` : ""}
        ${c.val ? `<span class="val">${esc(c.val)}</span>` : ""}
      </button>
      <span class="status">${on ? "" : "not in deck"}</span>
      <button class="del" data-del="${esc(row.id)}" title="Remove from the deck">&times;</button>
    </div>` + (S.editing === row.id ? editorHTML(row) : "");
  }).join("");
  $("deck-count").textContent = `(${S.deck.length} slides, ${n} for these answers)`;
  $("tally").textContent = n;
  $("tally-off").textContent = off ? ` · ${off} excluded` : "";
  renderSaveState();
}

const OPERATORS = [["eq", "is"], ["ne", "is not"], ["in", "is one of"],
["not_in", "is not one of"], ["exists", "is answered"]];

function valueControl(field, op, current) {
  const spec = S.fields[field] || {};
  if (op === "exists")
    return `<span style="color:var(--muted);font-size:12.5px">(any answer counts)</span>`;
  if (op === "in" || op === "not_in")
    return `<input type="text" data-v value="${esc([].concat(current || []).join(", "))}"
              placeholder="comma-separated values">`;
  if (spec.kind === "date")
    return `<input type="date" data-v data-date value="${esc(toISO(current))}">
            <span class="preview">${esc(humanDate(current) || "pick a date")}</span>`;
  const vals = spec.values || [];
  // The option values are JSON so that `false` stays a boolean rather than
  // becoming the string "false". `data-json` says so, because a reader that
  // guesses will unwrap booleans and leave strings wrapped in their quotes -
  // which is a condition that can never match anything.
  if (vals.length && vals.length <= 12)
    return `<select data-v data-json>${vals.map(v =>
      `<option value="${esc(JSON.stringify(v))}"${JSON.stringify(v) === JSON.stringify(current)
        ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  if (vals.length)
    return `<input type="text" data-v list="vals-${esc(field)}" value="${esc(current ?? "")}"
              placeholder="${vals.length} values seen — type to filter">
            <datalist id="vals-${esc(field)}">${vals.map(v =>
      `<option value="${esc(v)}">`).join("")}</datalist>`;
  return `<input type="text" data-v value="${esc(current ?? "")}" placeholder="value">`;
}

function editorHTML(row) {
  const when = row.when || {};
  const op = "exists" in when ? "exists"
    : (["eq", "ne", "in", "not_in"].find(k => k in when) || "eq");
  const field = when.field || "";
  const spec = S.fields[field];
  const names = Object.keys(S.fields);
  if (!names.length)
    return `<div class="editor"><div class="note warn">No answer fields yet.
      Upload a payload on the Build screen first — conditions are chosen from
      the fields the payloads actually contain, never typed.</div>
      <div class="acts"><button class="btn" data-cancel="1">Close</button></div></div>`;
  return `<div class="editor">
    <div class="line">
      <b style="font-size:12.5px">Include this slide when</b>
      <select data-f style="max-width:340px">
        <option value=""${field ? "" : " selected"}>always — no condition</option>
        ${names.map(f => `<option value="${esc(f)}"${f === field ? " selected" : ""}>${esc(label(f))}${S.fields[f].varies === false ? " (never varies)" : ""}</option>`).join("")}
      </select>
      ${field ? `<select data-o>${OPERATORS.map(([k, l]) =>
      `<option value="${k}"${k === op ? " selected" : ""}>${l}</option>`).join("")}</select>` : ""}
      ${field ? valueControl(field, op, when[op]) : ""}
    </div>
    ${field ? `<div class="full" title="the exact field name written to rules.json">${esc(field)}</div>` : ""}
    ${spec && spec.varies === false
      ? `<div class="note warn">Every payload answers this the same way
          (${esc(JSON.stringify(spec.eg))}), so a condition on it is always true
          or always false. Pick a field that varies.</div>`
      : `<div class="note">Fields and values come from the payloads, so a rule
          cannot quietly fail because a field name was mistyped.</div>`}
    <div class="acts">
      <button class="btn primary" data-apply="${esc(row.id)}">Apply</button>
      <button class="btn" data-cancel="1">Cancel</button>
    </div></div>`;
}

/** A control that says its value is JSON gets parsed; anything else is taken
 *  literally. Guessing was the bug: unwrapping only booleans left every string
 *  wrapped in its own quotes, so `AuditType is "New Audit Client"` was really
 *  `AuditType is "\"New Audit Client\""` and matched nothing. Typed input has
 *  to stay literal too, or someone typing `null` in a text box gets a null. */
function readValue(el) {
  if (el.dataset.date !== undefined) return fromISO(el.value);
  if (el.dataset.json === undefined) return el.value;
  try { return JSON.parse(el.value); } catch (_) { return el.value; }
}

// -------------------------------------------------------- rules: persistence
/** One save, reachable from Rules and from Values. Both write the same file -
 *  `rules.json` holds the deck and the bindings - so two handlers would be two
 *  chances to send a different payload. */
async function saveRules() {
  try {
    const r = await sendJSON(`/api/libraries/${S.lib}/rules`,
      { deck: S.deck, placeholders: S.bindings }, "PUT");
    S.dirty = false;
    const c = r.changed;
    const bits = [];
    if (c.added.length) bits.push(`${c.added.length} added`);
    if (c.removed.length) bits.push(`${c.removed.length} removed`);
    if (c.conditions.length) bits.push(`${c.conditions.length} condition(s) changed`);
    if (c.reordered) bits.push("reordered");
    // `changed` describes the deck only. Saying "nothing changed" after
    // someone edited a binding would be a lie about their own work.
    flash(bits.length
      ? `Saved: ${bits.join(", ")}${r.backup ? ` (previous kept as ${r.backup})` : ""}`
      : `Saved${r.backup ? ` — previous kept as ${r.backup}` : ""}`, "ok");
    renderSaveState();
    renderDeck();
  } catch (err) { flash(err.message, "bad"); }
}

function renderSaveState() {
  for (const id of ["rules-state", "values-state"]) {
    const el = $(id);
    if (!el) continue;
    el.textContent = S.dirty ? "unsaved changes" : "saved";
    el.className = "fld-note" + (S.dirty ? " warn" : "");
  }
}

$("rules-save").onclick = saveRules;
$("values-save").onclick = saveRules;

$("rules-reset").onclick = async () => {
  if (!confirm(
    "Start the rules over?\n\nEvery slide goes back in, in library order, with "
    + "no conditions — the state a freshly imported library is in.\n\n"
    + "Your placeholder bindings are kept, and the current rules are backed up, "
    + "so this can be undone from Earlier versions.")) return;
  try {
    const r = await sendJSON(`/api/libraries/${S.lib}/rules/reset`, {});
    S.deck = r.deck; S.bindings = r.placeholders; S.dirty = false;
    flash(`Rules reset — previous kept as ${r.backup || "(none)"}`, "ok");
    renderRules(); renderMapping(); renderBuild(); renderSaveState();
  } catch (err) { flash(err.message, "bad"); }
};

// ------------------------------------------------------- rules: suggestion
/** A very small markdown renderer - headings, lists, blockquotes, bold, code.
 *  The report is written by us and read by an author, so it needs to be
 *  legible, not complete. Everything is escaped before any formatting is
 *  applied: the report quotes field names and values that came from the
 *  author's own files, and those are text, not markup. */
function md(src) {
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of String(src).split("\n")) {
    const line = raw.replace(/\s+$/, "");
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const li = line.match(/^(\s*)[-*]\s+(.*)$/);
    const ol = line.match(/^(\d+)\.\s+(.*)$/);
    const q = line.match(/^>\s?(.*)$/);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
    else if (li) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li${li[1].length >= 2 ? ' class="sub"' : ""}>${inline(li[2])}</li>`);
    } else if (ol) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(ol[2])}</li>`);
    } else if (q) { closeList(); out.push(`<blockquote>${inline(q[1])}</blockquote>`); }
    else if (!line.trim()) { closeList(); }
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return out.join("");
}

const deckPicker = filePicker(".pptx", null);
deckPicker.multiple = true;
deckPicker.onchange = () => {
  if (deckPicker.files.length) askSuggest([...deckPicker.files]);
  deckPicker.value = "";
};
$("pick-decks").onclick = () => deckPicker.click();
$("sdrop").addEventListener("drop", (e) => {
  const files = [...(e.dataTransfer.files || [])]
    .filter(f => /\.pptx$/i.test(f.name) && !f.name.startsWith("~$"));
  if (files.length) askSuggest(files);
});
dropTarget($("sdrop"), null, () => { });   // just the hover styling

$("rules-suggest").onclick = () => {
  const p = $("suggest-panel");
  p.hidden = !p.hidden;
};
$("suggest-close").onclick = () => { $("suggest-panel").hidden = true; };

let lastSuggestion = "";
async function askSuggest(files) {
  const form = new FormData();
  files.forEach(f => form.append("decks", f));
  $("suggest-out").innerHTML = `<p class="fld-note">Reading ${files.length} deck(s)…</p>`;
  $("suggest-acts").hidden = true;
  try {
    const r = await api(`/api/libraries/${S.lib}/suggest`,
      { method: "POST", body: form });
    lastSuggestion = r.markdown;
    $("suggest-out").innerHTML = md(r.markdown);
    $("suggest-acts").hidden = false;
    $("suggest-note").textContent = r.incremental
      ? "only what these decks change — rules that already match are counted, not listed"
      : "the full walkthrough — this library has no rules yet";
    if (r.unpaired.length)
      flash(`${r.unpaired.length} deck(s) had no answer set: ${r.unpaired.slice(0, 3).join(", ")}`,
        "bad");
  } catch (err) {
    $("suggest-out").innerHTML = `<p class="fld-note warn">${esc(err.message)}</p>`;
  }
}

$("suggest-copy").onclick = async () => {
  try { await navigator.clipboard.writeText(lastSuggestion); flash("Copied", "ok"); }
  catch (_) { flash("Could not reach the clipboard — use Download instead", "bad"); }
};
$("suggest-download").onclick = () => {
  const blob = new Blob([lastSuggestion], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${S.lib}-suggested-rules.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

$("rules-history").onclick = async () => {
  const panel = $("rules-panel");
  if (!panel.hidden) { panel.hidden = true; return; }
  try {
    const rows = await getJSON(`/api/libraries/${S.lib}/rules/backups`);
    panel.innerHTML = rows.length ? `<div class="vlist">${rows.map(b => `
      <div class="vrow"><span class="w">${esc(b.when)}</span>
        <span>${b.slides ?? "?"} slides</span><span class="sp"></span>
        <button class="btn" data-restore="${esc(b.file)}">Restore</button></div>`).join("")}
      </div>` : `<p class="fld-note">No earlier versions yet. One is kept every
        time the rules are saved or reset.</p>`;
    panel.hidden = false;
  } catch (err) { flash(err.message, "bad"); }
};

async function restore(file) {
  try {
    const r = await sendJSON(`/api/libraries/${S.lib}/rules/restore`, { backup: file });
    S.deck = r.deck; S.bindings = r.placeholders; S.dirty = false;
    $("rules-panel").hidden = true;
    flash(`Restored ${file}`, "ok");
    renderRules(); renderMapping(); renderBuild(); renderSaveState();
  } catch (err) { flash(err.message, "bad"); }
}

// --------------------------------------------------------- 2. questions
const samplePicker = filePicker(".json", (f) => addSamples([f]));
samplePicker.multiple = true;
samplePicker.onchange = () => {
  if (samplePicker.files.length) addSamples([...samplePicker.files]);
  samplePicker.value = "";
};
$("qchoose").onclick = () => samplePicker.click();
dropTarget($("qdrop"), ".json", (f) => addSamples([f]));
// dropTarget hands over one file; take the whole drop, since "several at once"
// is the entire point of this screen
$("qdrop").addEventListener("drop", (e) => {
  const files = [...(e.dataTransfer.files || [])].filter(f => /\.json$/i.test(f.name));
  if (files.length > 1) addSamples(files.slice(1));
});

async function addSamples(files) {
  if (!S.lib) { flash("Import a library first — answer sets belong to one", "bad"); return; }
  let cat = null;
  for (const file of files) {
    try {
      const answers = JSON.parse(await file.text());
      cat = await sendJSON(`/api/libraries/${S.lib}/payloads`,
        { name: file.name.replace(/\.json$/i, ""), answers });
    } catch (err) {
      flash(`${file.name}: ${err.message}`, "bad");
    }
  }
  if (!cat) return;
  S.fields = cat.fields; S.presets = cat.payloads; S.shared = cat.shared;
  fillPresets();
  // "Added" and "Replaced" look identical otherwise, and replacing an answer
  // set by re-using its name is exactly the thing worth noticing.
  flash(`${cat.replaced ? "Replaced" : "Added"} ${cat.saved} — now `
    + `${S.presets.length} answer set(s), ${Object.keys(S.fields).length} field(s)`, "ok");
  renderQuestions(); renderRules(); renderMapping(); renderBuild();
}

async function dropSample(labelName) {
  if (!confirm(`Remove the answer set "${labelName}"?\n\nThe questions and `
    + `values it contributed go with it. Rules already written are untouched, `
    + `but a condition on a field only this set mentioned will have no field `
    + `to show.`)) return;
  try {
    const cat = await sendJSON(
      `/api/libraries/${S.lib}/payloads/${encodeURIComponent(labelName)}`,
      null, "DELETE");
    S.fields = cat.fields; S.presets = cat.payloads; S.shared = cat.shared;
    fillPresets();
    flash(`Removed ${labelName} — ${Object.keys(S.fields).length} field(s) left`, "ok");
    renderQuestions(); renderRules(); renderMapping(); renderBuild();
  } catch (err) { flash(err.message, "bad"); }
}

function renderQuestions() {
  const names = Object.keys(S.fields);
  // Name each one and let it be removed. Adding is by name - a new name is
  // kept alongside, the same name replaces - and a wrong set is not harmless:
  // every field and value in it joins the catalogue, so it can put a question
  // in the condition editor that no real request contains.
  $("qcurrent").innerHTML = S.presets.length
    ? `<div class="vlist" style="width:100%">
         ${S.presets.map(p => `<div class="vrow">
           <span class="f">${esc(p.label)}</span>
           <span class="m">${Object.keys(p.answers || {}).length} answers</span>
           <span class="sp"></span>
           ${S.shared ? `<span class="pill warn">shared</span>`
        : `<button class="btn danger" data-drop="${esc(p.label)}">Remove</button>`}
         </div>`).join("")}
       </div>`
    : "";
  $("q-count").textContent = names.length ? `(${names.length})` : "";

  // A field answered identically everywhere cannot explain anything, so a
  // condition on it is always true or always false. Say so here rather than
  // letting someone discover it after writing the rule.
  const fixed = names.filter(f => S.fields[f].varies === false);
  const warn = $("q-warn");
  warn.innerHTML = S.shared && S.presets.length
    ? `<b>These answer sets are shared, not this library's</b>
       They were uploaded before answer sets belonged to a library, so every
       library still sees them. Drop them here again to attach them to
       <code>${esc(S.lib)}</code> — until then another template's questions can
       appear in this one's condition editor.`
    : !names.length
    ? `<b>No answer sets yet</b> Drop the JSON files real requests arrive as.
       Until then the condition editor has no fields to offer and the build form
       has no questions.`
    : fixed.length
      ? `<b>${fixed.length} field(s) never vary</b>
         ${fixed.map(f => `<code>${esc(f)}</code>`).join(", ")} — answered the same
         way in every set here, so a condition on one is always true or always
         false. Add a set that answers it differently, or leave it alone.`
      : "";
  warn.hidden = !warn.innerHTML;

  $("q-body").innerHTML = names.map(f => {
    const s = S.fields[f];
    const vals = (s.values || []).map(v => `<span class="tag">${esc(String(v))}</span>`);
    return `<tr><td class="ph" title="${esc(f)}">${esc(f)}</td>
      <td class="used">${esc(s.kind)}</td>
      <td>${vals.length ? `<div class="tags">${vals.slice(0, 8).join("")}
        ${vals.length > 8 ? `<span class="tag">+${vals.length - 8} more</span>` : ""}</div>`
        : `<span class="preview">free text</span>`}</td>
      <td class="preview">${esc(s.kind === "date" ? humanDate(s.eg) : String(s.eg ?? ""))}</td>
      <td>${s.varies === false
        ? `<span class="pill warn">never varies</span>`
        : `<span class="pill ok">usable</span>`}</td></tr>`;
  }).join("");
}

// ---------------------------------------------------------- 3. mapping
function renderMapping() {
  const i = S.inspect;
  if (!i) return;
  const used = {};
  i.blocks.forEach(b => b.placeholders.forEach(p => used[p] = (used[p] || 0) + 1));
  const names = Object.keys(S.fields);
  $("map-note").innerHTML = names.length
    ? `<b>Field catalogue from ${S.presets.length} payload(s)</b>
       ${names.length} fields, each with the values actually seen. The same list
       fills the condition dropdowns and the build form.`
    : `<b>No payloads loaded yet</b> Upload one on the Build screen and this
       becomes a list of real fields instead of free text.`;
  $("map-note").className = "notice " + (names.length ? "ok" : "warn");

  const DATE_FORMATS = {
    long_comma: "November 30, 2026", day_month: "30 November 2026",
    abbr_comma: "Nov 30, 2026", day_abbr: "30 Nov 2026",
    us_slash: "11/30/2026", eu_slash: "30/11/2026",
    iso: "2026-11-30", raw: "20261130",
  };

  $("map-count").textContent = `(${Object.keys(used).length})`;
  $("map-body").innerHTML = Object.keys(used).sort().map(p => {
    const key = `{{${p}}}`;
    const bind = S.bindings[key];
    const pick = (sel) => `<select data-bind="${esc(key)}" data-part="field">
      <option value="">choose a field…</option>
      ${names.map(f => `<option${f === sel ? " selected" : ""} title="${esc(f)}">${esc(label(f))}</option>`).join("")}
      </select>`;
    const kind = (k) => `<select data-bind="${esc(key)}" data-part="from">
      ${["field", "data", "literal"].map(o =>
      `<option${o === k ? " selected" : ""}>${o}</option>`).join("")}</select>`;
    let from, renders, state;
    if (!bind) {
      from = `${kind("")}`;
      renders = `<span class="preview">nothing — the deck shows ${esc(key)}</span>`;
      state = `<span class="pill bad">unbound</span>`;
    } else if (bind.from === "field") {
      const spec = S.fields[bind.field] || {};
      // Offer the format whenever the field holds a date, not only when one has
      // already been chosen. Showing it only for bindings that already had a
      // format made it unreachable: an imported library's starter binding has
      // none, so the deck printed 20261130 and there was no control anywhere to
      // say otherwise.
      const fmt = spec.kind === "date"
        ? `<select data-bind="${esc(key)}" data-part="format">
             <option value=""${bind.format ? "" : " selected"}>as stored (${esc(spec.eg ?? "")})</option>
             ${Object.entries(DATE_FORMATS).map(([k, v]) =>
          `<option value="${k}"${k === bind.format ? " selected" : ""}>${v}</option>`).join("")}
           </select>` : "";
      from = `${kind("field")} ${pick(bind.field)} ${fmt}`;
      renders = `<span class="preview">${esc(bind.format ? formatted(spec.eg, bind.format)
        : (spec.eg ?? (spec.values || ["—"])[0] ?? "—"))}</span>`;
      state = names.includes(bind.field)
        ? `<span class="pill ok">bound</span>`
        : `<span class="pill warn">no such field</span>`;
    } else if (bind.from === "data") {
      from = `${kind("data")} <code style="font-size:12px">${esc(bind.source)} / ${esc(bind.key)}</code>`;
      renders = `<span class="preview">needs an external system</span>`;
      state = `<span class="pill warn">not wired</span>`;
    } else {
      from = `${kind("literal")} <input type="text" data-bind="${esc(key)}"
        data-part="value" value="${esc(bind.value ?? "")}" size="16">`;
      renders = `<span class="preview">${esc(bind.value ?? "")}</span>`;
      state = `<span class="pill ok">bound</span>`;
    }
    return `<tr><td class="ph">${esc(key)}</td>
      <td class="used">${used[p]} slide${used[p] > 1 ? "s" : ""}</td>
      <td>${from}</td><td>${renders}</td><td>${state}</td></tr>`;
  }).join("");
}

// ------------------------------------------------------------- 4. build
function renderBuild() {
  const names = Object.keys(S.fields);
  $("build-src").textContent = S.lib
    ? `${S.lib} · ${names.length} question(s)` : "";
  $("answers-form").innerHTML = names.length
    ? `<div class="formgrid">` + names.map(f => {
      const spec = S.fields[f], cur = S.answers[f];
      if (spec.kind === "bool")
        return `<label class="q bool"><input type="checkbox" data-ans="${esc(f)}"${cur === true ? " checked" : ""}>
          <span class="qt">${esc(label(f))}</span></label>`;
      let ctl;
      if (spec.kind === "date")
        ctl = `<input type="date" data-ans="${esc(f)}" data-date value="${esc(toISO(cur))}">`;
      else if ((spec.values || []).length && spec.values.length <= 12)
        ctl = `<select data-ans="${esc(f)}"><option value=""></option>${spec.values.map(v =>
          `<option${String(v) === String(cur) ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
      else if ((spec.values || []).length)
        ctl = `<input type="text" data-ans="${esc(f)}" list="q-${esc(f)}" value="${esc(cur ?? "")}"
                 placeholder="${spec.values.length} options">
               <datalist id="q-${esc(f)}">${spec.values.map(v => `<option value="${esc(v)}">`).join("")}</datalist>`;
      else
        ctl = `<input type="text" data-ans="${esc(f)}" value="${esc(cur ?? "")}"
                 placeholder="${esc(spec.eg ?? "")}">`;
      return `<label class="q"><span class="qt" title="${esc(f)}">${esc(label(f))}</span>${ctl}</label>`;
    }).join("") + `</div>`
    : `<p class="fld-note">No questions yet — upload a payload and the form is
       built from the fields it contains.</p>`;
  renderBuildResult();
}

/** The preview comes from the server, not from `holds()`. The local check is
 *  good enough to grey a row while you drag, but what you are about to
 *  download must be decided by the same code that will build it. */
let selectSeq = 0;
async function renderBuildResult() {
  if (!S.lib) return;
  const mine = ++selectSeq;
  let sel;
  try {
    sel = await sendJSON(`/api/libraries/${S.lib}/select`, { answers: S.answers });
  } catch (err) {
    $("build-list").innerHTML = `<p class="fld-note">${esc(err.message)}</p>`;
    $("build-count").textContent = "";
    return;
  }
  if (mine !== selectSeq) return;         // a newer answer won the race
  $("build-count").textContent = `(${sel.count} slides)`;
  $("build-list").innerHTML = sel.slides.map((s, i) =>
    `<div class="bl"><span class="n">${i + 1}</span>
      <span class="t" title="${esc(s.block)}">${esc(s.title)}</span></div>`).join("");
  // Two different problems with two different owners. A binding aimed at a
  // field nobody answers is broken for everyone and belongs to the author; a
  // field left blank is this user's answer and is often deliberate. Showing
  // them the same way sends the wrong person looking.
  const warn = $("build-warn");
  warn.innerHTML = sel.unbound.length
    ? `<b>${sel.unbound.length} placeholder(s) will print as literal text</b>
       ${sel.unbound.map(p => "{{" + esc(p) + "}}").join(", ")} —
       either nothing is bound, or the binding names a field the answers do not
       contain. Fix it on the Mapping screen.` : "";
  warn.hidden = !sel.unbound.length;

  const note = $("build-empty");
  const empty = sel.empty || [];
  note.innerHTML = empty.length
    ? `<b>${empty.length} placeholder(s) resolve to nothing</b>
       ${empty.map(p => "{{" + esc(p) + "}}").join(", ")} — bound correctly, but
       these answers leave them blank. Often intended; worth a look.` : "";
  note.hidden = !empty.length;
}

function invalidateBuild() {
  S.built = null;
  S.slides = [];
  if ($("viewer-panel")) $("viewer-panel").hidden = true;
  $("build-dl").disabled = true;
  $("build-status").textContent = "answers changed — build again";
}

document.querySelectorAll("[data-answers]").forEach(b => b.onclick = () => {
  document.querySelectorAll("[data-answers]").forEach(x =>
    x.classList.toggle("on", x === b));
  $("answers-form").hidden = b.dataset.answers !== "form";
  $("answers-upload").hidden = b.dataset.answers !== "upload";
});

/* Two uploads that look alike and are not.
 *
 * On the Questions screen the AUTHOR declares what users will be asked: the
 * file is kept, and the field catalogue is rebuilt from every answer set held.
 * On the Build screen a USER supplies answers for one deck: the file fills the
 * form and is not kept, because someone building a proposal should not be able
 * to change the template's questions by dropping a file on it.
 *
 * The same handler did both, which is why the author had to visit the user's
 * screen to get started.
 */
const answersPicker = filePicker(".json", useAnswers);
dropTarget($("adrop"), ".json", useAnswers);
// The button is in the markup, not injected here. Rewriting someone else's
// markup at load means a change to the HTML takes the whole page down at the
// first line that assumes a shape - which is how this file already lost a
// screen once.
$("pick-answers").onclick = () => answersPicker.click();

async function useAnswers(file) {
  try {
    S.answers = { ...JSON.parse(await file.text()) };
    flash(`Answers loaded from ${file.name}`, "ok");
    renderBuild(); renderDeck(); renderTest(); invalidateBuild();
    document.querySelector('[data-answers="form"]').click();
  } catch (err) {
    flash(`${file.name} is not an answer set: ${err.message}`, "bad");
  }
}

function fillPresets() {
  const opts = `<option value="">choose…</option>` + S.presets.map((p, i) =>
    `<option value="${i}">${esc(p.label)}</option>`).join("");
  $("build-preset").innerHTML = opts;
  $("preset").innerHTML = opts;
  $("preset-wrap").hidden = !S.presets.length;
}

function usePreset(i) {
  if (i === "" || !S.presets[i]) return;
  S.answers = { ...S.presets[i].answers };
  renderBuild(); renderDeck(); renderTest(); invalidateBuild();
}
$("build-preset").onchange = function () { usePreset(this.value); };
$("preset").onchange = function () {
  usePreset(this.value);
  if (!$("testbar").classList.contains("open")) $("inputs-toggle").click();
};

/* -------------------------------------------------------- the deck viewer
 *
 * A slide arrives one of two ways and the difference matters to whoever is
 * looking. `png` is the slide exported by PowerPoint — the real thing. `svg` is
 * our own renderer, which is an approximation and says so in the panel header;
 * on a template that leans on inherited styling it can come back looking like a
 * skeleton, so it must never be mistaken for what the client receives.
 */
function big(s) {
  if (s.png) return `<img class="slide" src="${s.png}" alt="Slide ${s.index}">`;
  if (s.svg) return s.svg.replace("<svg ", '<svg class="slide" ');
  return `<div class="slide-fail">This slide did not render.<br>
    <span class="fld-note">${esc(s.error || "")}</span><br>
    <span class="fld-note">It is still in the file — only the preview
    failed.</span></div>`;
}

function small(s) {
  // Lazy: a 60-slide filmstrip is 60 images, and only a handful are on screen.
  if (s.png) return `<img class="fthumb" loading="lazy" src="${s.png}" alt="">`;
  if (s.svg) return s.svg.replace("<svg ", '<svg class="fthumb" ');
  return '<span class="fthumb fail"></span>';
}

function renderViewer() {
  const panel = $("viewer-panel");
  if (!S.slides || !S.slides.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const at = Math.max(0, Math.min(S.slideAt || 0, S.slides.length - 1));
  S.slideAt = at;
  const s = S.slides[at];

  $("viewer-count").textContent = `(${at + 1} of ${S.slides.length})`;
  $("viewer-stage").innerHTML = big(s);
  $("viewer-prev").disabled = at === 0;
  $("viewer-next").disabled = at === S.slides.length - 1;

  $("viewer-strip").innerHTML = S.slides.map((x, i) => `
    <button class="frame ${i === at ? "on" : ""}" data-slide="${i}"
            title="${esc(x.title || "")}">
      <span class="fno">${i + 1}</span>
      ${small(x)}
    </button>`).join("");
  const active = $("viewer-strip").querySelector(".frame.on");
  if (active && active.scrollIntoView)
    active.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function moveSlide(by) {
  if (!S.slides || !S.slides.length) return;
  S.slideAt = Math.max(0, Math.min((S.slideAt || 0) + by, S.slides.length - 1));
  renderViewer();
}
$("viewer-prev").onclick = () => moveSlide(-1);
$("viewer-next").onclick = () => moveSlide(1);
document.addEventListener("click", (e) => {
  const f = e.target.closest("[data-slide]");
  if (f) { S.slideAt = +f.dataset.slide; renderViewer(); }
});
// Arrow keys, but never while someone is typing an answer into a form.
document.addEventListener("keydown", (e) => {
  if ($("viewer-panel").hidden) return;
  const t = e.target;
  if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName || "")) return;
  if (e.key === "ArrowLeft") { moveSlide(-1); e.preventDefault(); }
  if (e.key === "ArrowRight") { moveSlide(1); e.preventDefault(); }
});

async function loadBuiltSlides(token) {
  const panel = $("viewer-panel"), note = $("viewer-how");
  panel.hidden = false;
  $("viewer-count").textContent = "";
  $("viewer-strip").innerHTML = "";
  $("viewer-stage").innerHTML =
    `<div class="slide-wait">Rendering the deck…<br>
     <span class="fld-note">PowerPoint is drawing each slide. First look at a
     deck takes a moment; after that it is instant.</span></div>`;
  note.textContent = "";
  try {
    const r = await getJSON(`/api/builds/${token}/slides`);
    S.slides = r.slides || [];
    S.slideAt = 0;
    note.className = r.engine === "powerpoint" ? "hint" : "hint warn-text";
    note.innerHTML = r.engine === "powerpoint"
      ? "rendered by PowerPoint &middot; arrow keys to move"
      : `approximation, not PowerPoint &mdash; ${esc(r.why || "PowerPoint is "
        + "not available here")}. Check the downloaded file before trusting how
         this looks.`;
    renderViewer();
    const broke = S.slides.filter(s => !s.png && !s.svg).length;
    if (broke) flash(`${broke} slide(s) could not be previewed — they are still `
      + `in the downloaded file`, "bad");
  } catch (err) {
    S.slides = []; renderViewer();
    flash(`Built, but the preview failed: ${err.message}`, "bad");
  }
}

$("build-go").onclick = async () => {
  try {
    $("build-status").textContent = "building…";
    const r = await sendJSON(`/api/libraries/${S.lib}/build`, { answers: S.answers });
    S.built = r;
    $("build-dl").disabled = false;
    $("build-status").textContent =
      `built — ${r.slides} slides. The same answers always give this deck.`;
    flash(`Built ${r.slides} slides`, "ok");
    loadBuiltSlides(r.token);
  } catch (err) {
    $("build-status").textContent = "";
    flash(err.message, "bad");
  }
};
$("build-dl").onclick = () => {
  if (!S.built) return;
  const a = document.createElement("a");
  a.href = S.built.download;
  a.download = S.built.filename || "proposal.pptx";
  a.click();
};

// -------------------------------------------------------------- 5. check
document.querySelectorAll("[data-slot]").forEach(slot => {
  const which = slot.dataset.slot;
  const dz = slot.querySelector(".dropzone");
  const picker = filePicker(".pptx", f => takeDeck(which, f));
  dropTarget(dz, ".pptx", f => takeDeck(which, f));
  slot.querySelector("button").onclick = () => picker.click();
});

function takeDeck(which, file) {
  S.check[which] = file;
  const slot = document.querySelector(`[data-slot="${which}"] .dz-main`);
  slot.innerHTML = `<b>${esc(file.name)}</b>`;
  $("run-check").disabled = !(S.check.ours && S.check.templafy);
}

$("run-check").onclick = async () => {
  if (!(S.check.ours && S.check.templafy)) {
    flash("Drop both decks first", "bad"); return;
  }
  const form = new FormData();
  form.append("ours", S.check.ours);
  form.append("templafy", S.check.templafy);
  form.append("lib", S.lib || "");
  $("check-result").innerHTML = `<div class="pad fld-note">Comparing…</div>`;
  try {
    renderCheck(await api("/api/compare", { method: "POST", body: form }));
  } catch (err) {
    $("check-result").innerHTML = `<div class="pad"><span class="pill bad">${esc(err.message)}</span></div>`;
  }
};

function renderCheck(r) {
  const tone = r.exact ? "ok" : (r.selection_ok ? "warn" : "bad");
  const parts = [`<div class="verdict ${tone}">
    <span class="big">${esc(r.verdict)}</span>
    <span>${r.our_slides} slides ours · ${r.templafy_slides} Templafy's · ${r.matched} matched</span>
    <span class="files">${esc(S.check.ours.name)} vs ${esc(S.check.templafy.name)}</span></div>`];

  const how = { creationId: "exact — the shape ids match", structural: "layout and geometry — survives placeholder filling", text: "text overlap — only meaningful on static slides" };
  parts.push(`<div class="finding"><h3>How the slides were paired
    <span class="why">strongest signal available, one slide to one slide</span></h3>
    <div class="dl">${Object.entries(r.by_method || {}).map(([m, n]) =>
    `<div class="di"><span class="b">${n}</span><span class="k">${esc(m)}</span>
      <span class="p">${esc(how[m] || "")}</span></div>`).join("")
    || `<div class="di"><span class="p">nothing matched</span></div>`}</div></div>`);

  const sect = (t, n, why, items) => `<div class="finding"><h3>${esc(t)}
    ${n ? `<span class="n">${n}</span>` : ""}<span class="why">${esc(why)}</span></h3>
    ${items.length ? `<div class="dl">${items.join("")}</div>` : ""}</div>`;
  const item = (b, k, p) => `<div class="di"><span class="b">${esc(b)}</span>
    <span class="k">${esc(k)}</span><span class="p">${esc(p || "")}</span></div>`;

  if (r.missing.length || r.extra.length)
    parts.push(sect("Wrong slides selected", r.missing.length + r.extra.length,
      "the rules put a different set of slides in the deck — this is a rules fix",
      r.missing.map(s => item("only in Templafy's", "#" + s.index, s.preview))
        .concat(r.extra.map(s => item("only in ours", "#" + s.index, s.preview)))));

  if ((r.displaced || []).length)
    parts.push(sect("Slides in the wrong place", r.out_of_order,
      "the right slides, ordered differently — drag the named block on the Rules screen",
      r.displaced.map(d => item(d.block || "unnamed block",
        `Templafy #${d.templafy_index} → ours #${d.our_index}`, d.preview))));

  if (r.text_differs.length)
    parts.push(`<div class="finding"><h3>Text differences
      <span class="n">${r.text_differs.length}</span>
      <span class="why">same slide, different words — usually an unbound placeholder</span></h3>
      <div class="dl">${r.text_differs.map(t => `<div class="di" style="display:block">
        <span class="k">slide #${t.ours_index}</span>
        ${(t.segments || []).map(s => `<div class="seg">
          <div class="mine">ours: ${esc(s.ours || "(nothing)")}</div>
          <div class="theirs">Templafy: ${esc(s.templafy || "(nothing)")}</div></div>`).join("")}
      </div>`).join("")}</div></div>`);

  if (r.data_driven)
    parts.push(sect("Regenerated per deck", r.data_driven,
      "Templafy rebuilds these with fresh shape ids, so they cannot be paired by "
      + "identity even when selection is perfect. Counted separately, not as errors.", []));

  if (r.exact)
    parts.push(`<div class="finding"><h3>No differences
      <span class="why">same slides, same order, same text</span></h3></div>`);

  parts.push(`<div class="foot">Matched by shape creationId, then layout and
    geometry, then text — whichever is strongest. Both files were read on this
    machine and neither was kept.</div>`);
  $("check-result").innerHTML = parts.join("");
}

// ------------------------------------------------------------ inputs bar
function renderTest() {
  const used = [];
  S.deck.forEach(r => { if (r.when && !used.includes(r.when.field)) used.push(r.when.field); });
  $("inputs-n").textContent = used.length;
  $("test-hint").textContent = used.length
    ? "the answers that can change this deck"
    : "no conditions yet — every slide is always in";
  $("test-body").innerHTML = used.map(f => {
    const spec = S.fields[f] || {}, cur = S.answers[f];
    if (spec.kind === "bool")
      return `<label class="ans bool" title="${esc(f)}">
        <input type="checkbox" data-ans="${esc(f)}"${cur === true ? " checked" : ""}>
        <span>${esc(label(f))}</span></label>`;
    const vals = spec.values || [];
    const ctl = spec.kind === "date"
      ? `<input type="date" data-ans="${esc(f)}" data-date value="${esc(toISO(cur))}">`
      : vals.length
        ? `<select data-ans="${esc(f)}"><option value=""></option>${vals.map(v =>
          `<option${String(v) === String(cur) ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`
        : `<input type="text" data-ans="${esc(f)}" value="${esc(cur ?? "")}" placeholder="${esc(spec.eg ?? "")}">`;
    return `<label class="ans" title="${esc(f)}"><span class="n">${esc(label(f))}</span>${ctl}</label>`;
  }).join("");
}

const bar = $("testbar");
$("inputs-toggle").onclick = function () {
  const open = bar.classList.toggle("open");
  $("test-body").hidden = !open;
  this.setAttribute("aria-expanded", open ? "true" : "false");
  fitMain();
};

// ----------------------------------------------------------------- events
$("pool-search").oninput = renderPool;

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-add],[data-del],[data-edit],[data-cancel],[data-apply],[data-restore],[data-drop]");
  if (!t) return;
  if (t.dataset.restore !== undefined) { restore(t.dataset.restore); return; }
  if (t.dataset.drop !== undefined) { dropSample(t.dataset.drop); return; }
  if (t.dataset.add !== undefined) { S.deck.push({ id: t.dataset.add, when: null }); S.dirty = true; }
  else if (t.dataset.del !== undefined) { S.deck = S.deck.filter(r => r.id !== t.dataset.del); S.dirty = true; }
  else if (t.dataset.edit !== undefined) {
    S.editing = S.editing === t.dataset.edit ? null : t.dataset.edit; renderDeck(); return;
  } else if (t.dataset.cancel !== undefined) { S.editing = null; renderDeck(); return; }
  else if (t.dataset.apply !== undefined) {
    const ed = t.closest(".editor");
    const field = ed.querySelector("[data-f]").value;
    const row = S.deck.find(r => r.id === t.dataset.apply);
    if (!field) row.when = null;
    else {
      const op = ed.querySelector("[data-o]").value;
      const v = ed.querySelector("[data-v]");
      if (op === "exists") row.when = { field, exists: true };
      else if (op === "in" || op === "not_in")
        row.when = { field, [op]: v.value.split(",").map(s => s.trim()).filter(Boolean) };
      else row.when = { field, [op]: readValue(v) };
    }
    S.editing = null; S.dirty = true;
  }
  renderPool(); renderDeck(); renderTest(); renderBuildResult();
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.matches("[data-f],[data-o]")) {
    const ed = t.closest(".editor");
    const row = S.deck.find(r => r.id === S.editing);
    const field = ed.querySelector("[data-f]").value;
    const op = (ed.querySelector("[data-o]") || {}).value || "eq";
    row.when = !field ? null
      : (op === "exists" ? { field, exists: true } : { field, [op]: "" });
    renderDeck();
    return;
  }
  if (t.dataset.ans !== undefined) {
    S.answers[t.dataset.ans] = t.type === "checkbox" ? t.checked
      : t.dataset.date !== undefined ? fromISO(t.value) : t.value;
    renderDeck(); renderBuildResult(); invalidateBuild();
    return;
  }
  if (t.dataset.bind !== undefined) {
    const key = t.dataset.bind, part = t.dataset.part;
    const bind = S.bindings[key] || {};
    if (part === "from") {
      S.bindings[key] = t.value === "field" ? { from: "field", field: "" }
        : t.value === "literal" ? { from: "literal", value: "" }
          : t.value === "data" ? { from: "data", source: "", key: "" } : undefined;
      if (!t.value) delete S.bindings[key];
    } else if (part === "field") {
      // The <option> shows a readable label; the value written must be the
      // exact field name, or a binding silently resolves to nothing.
      const chosen = Object.keys(S.fields).find(f => label(f) === t.options[t.selectedIndex].text);
      S.bindings[key] = { ...bind, from: "field", field: chosen || t.value };
    } else if (part === "format") {
      // "as stored" means no format key at all, not format:"" - bindings.py
      // would try to render an empty format name and get nothing.
      const next = { ...bind };
      if (t.value) next.format = t.value; else delete next.format;
      S.bindings[key] = next;
    } else if (part === "value") {
      S.bindings[key] = { ...bind, from: "literal", value: t.value };
    }
    S.dirty = true;
    renderMapping(); renderDeck(); renderBuildResult(); renderSaveState();
  }
});

// ------------------------------------------------------------ drag & drop
let DRAG = null;
document.addEventListener("dragstart", (e) => {
  const item = e.target.closest("[data-id][draggable]");
  if (!item) return;
  DRAG = item.dataset.id;
  e.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragover", (e) => {
  const row = e.target.closest(".row");
  if (!DRAG || !row) return;
  e.preventDefault();
  document.querySelectorAll(".row.drop-before").forEach(r => r.classList.remove("drop-before"));
  row.classList.add("drop-before");
});
document.addEventListener("drop", (e) => {
  if (!DRAG || !e.target.closest("#deck")) return;
  e.preventDefault();
  const row = e.target.closest(".row");
  const entry = S.deck.find(r => r.id === DRAG) || { id: DRAG, when: null };
  S.deck = S.deck.filter(r => r.id !== DRAG);
  const at = row ? S.deck.findIndex(r => r.id === row.dataset.id) : -1;
  S.deck.splice(at < 0 ? S.deck.length : at, 0, entry);
  DRAG = null; S.dirty = true;
  renderPool(); renderDeck(); renderTest(); renderBuildResult();
});
document.addEventListener("dragend", () => {
  document.querySelectorAll(".row.drop-before").forEach(r => r.classList.remove("drop-before"));
  DRAG = null;
});

// Leaving with unsaved rules loses them - rules.json is only written on save.
window.addEventListener("beforeunload", (e) => {
  if (S.dirty) { e.preventDefault(); e.returnValue = ""; }
});

// ------------------------------------------------------------------- boot
(async function boot() {
  applyTheme();
  $("run-check").disabled = true;
  $("source-badge").textContent = "LIVE";
  $("source-badge").classList.add("real");
  try {
    await loadLibraries();
  } catch (err) { flash(err.message, "bad"); }
  showTab("library");
})();
