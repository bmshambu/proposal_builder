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
  input.multiple = accept.includes(",");     // a library is a .pptx AND its .pdf
  input.hidden = true;
  input.onchange = () => {
    [...input.files].forEach(onFile);
    input.value = "";
  };
  document.body.appendChild(input);
  return input;
}

/** ".pptx" or ".pptx,.pdf" - does this filename match? */
function accepts(accept, name) {
  return !accept || accept.split(",").some(ext =>
    name.toLowerCase().endsWith(ext.trim()));
}

function dropTarget(el, accept, onFile) {
  ["dragenter", "dragover"].forEach(ev => el.addEventListener(ev, e => {
    e.preventDefault(); el.classList.add("over");
  }));
  ["dragleave", "drop"].forEach(ev => el.addEventListener(ev, e => {
    e.preventDefault(); el.classList.remove("over");
  }));
  el.addEventListener("drop", e => {
    const files = [...e.dataTransfer.files];
    if (!files.length) return;
    for (const f of files) {
      if (!accepts(accept, f.name)) {
        flash(`${f.name} is not a ${accept} file`, "bad");
        continue;
      }
      onFile(f);
    }
  });
}

// ------------------------------------------------------------------ state
const S = {
  libs: [], lib: null,
  inspect: null, thumbs: {},          // block id -> svg string
  deck: [], bindings: {},
  fields: {}, presets: [],
  answers: {},
  dirty: false, editing: null, moving: null, built: null,
  condRows: 1, condJoin: "all",       // the open editor's clause rows and join
  check: { ours: null, templafy: null },
  slides: [], slideAt: 0,        // the built deck, for the viewer
  stage: { pptx: null, pdf: null },   // an import waiting for both its files
};

const block = (id) => (S.inspect ? S.inspect.blocks.find(b => b.id === id) : null);

/** The slide's number in library.pptx - the one number on this screen that is
 *  not a position in something. A row's position changes every time the order
 *  does; this does not, which is what makes it the thing to read against the
 *  same deck open in Templafy or PowerPoint. Shown as `s47`, never bare, so it
 *  can never be mistaken for the position beside it. */
const slideNo = (id) => { const b = block(id); return b ? b.number : null; };
const srcHTML = (id) => {
  const n = slideNo(id);
  return n ? `<span class="src" title="Slide ${n} in library.pptx">s${n}</span>`
           : `<span class="src none" title="not in this library">s?</span>`;
};
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
  // The slide text is fetched when the screen is opened, not at boot: an
  // author who never marks anything should never pay for it.
  if (tab === "mark") renderMark();
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
  // A sidecar id is as stable as a marker - it is pinned to the slide, not to
  // its heading - and a firm template has no markers in it at all. Counting
  // only markers reported "0 stable ids" on a 130-slide deck where every one
  // of them was named, and left the six figures not adding up to the slide
  // count.
  const stable = (by.marker || 0) + (by.map || 0);
  $("stats").innerHTML = `
    <div class="stat"><b>${i.blocks.length}</b><span>slides found</span></div>
    <div class="stat ok"><b>${stable}</b><span>stable ids</span></div>
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
/* A library is two files, and they arrive in whatever order they arrive in.
 *
 * The .pptx is the library. The .pdf is what the deck viewer previews from -
 * pages copied out of it, mirroring the slides copied out of the .pptx - which
 * is what lets previews work with no converter installed anywhere. So both are
 * staged first and imported together, rather than the .pptx importing on drop
 * and the .pdf arriving too late to be part of it.
 */
// Still accepts a .pdf: an author who has one made by PowerPoint should be
// able to use it, because it is a truer picture than a converted one. It is
// no longer asked for.
const libPicker = filePicker(".pptx,.pdf", stageLibraryFile);
$("choose").onclick = () => libPicker.click();
dropTarget($("dropzone"), ".pptx,.pdf", stageLibraryFile);

function stageLibraryFile(file) {
  const ext = file.name.toLowerCase().split(".").pop();
  if (ext === "pptx") S.stage.pptx = file;
  else if (ext === "pdf") S.stage.pdf = file;
  else return flash(`${file.name} is not a .pptx or .pdf`, "bad");
  renderStaged();
}

function renderStaged() {
  const box = $("staged"), st = S.stage;
  if (!st.pptx && !st.pdf) { box.innerHTML = ""; box.hidden = true; return; }
  box.hidden = false;
  const row = (label, file, note) => `
    <div class="stage-row ${file ? "on" : ""}">
      <span class="stage-tick">${file ? "&#10003;" : "&#8213;"}</span>
      <b>${label}</b>
      <span class="m">${file ? esc(file.name) : note}</span>
    </div>`;
  box.innerHTML =
    row("library .pptx", st.pptx, "required")
    + row("library .pdf", st.pdf,
      "optional — one is made on import. Bring your own and it wins: a PDF "
      + "out of PowerPoint is a truer picture than a converted one")
    + `<div class="stage-go">
         <button class="btn primary" id="stage-import"
                 ${st.pptx ? "" : "disabled"}>Import library</button>
         <button class="btn" id="stage-clear">Clear</button>
       </div>`;
  $("stage-import").onclick = () => uploadLibrary();
  $("stage-clear").onclick = () => { S.stage = { pptx: null, pdf: null }; renderStaged(); };
}

async function uploadLibrary() {
  const file = S.stage.pptx;
  if (!file) return flash("Drop the library .pptx first", "bad");
  const name = ($("lib-name").value || "").trim();
  if (!/^[a-z0-9_]{1,64}$/.test(name)) {
    flash("Give the library a name first — lower case, digits and underscores", "bad");
    $("lib-name").focus();
    return;
  }
  const exists = S.libs.some(l => l.id === name);
  if (exists && !confirm(
    `"${name}" already exists.\n\nThe deck is replaced. Its rules, answer sets `
    + `and earlier versions are kept, and the block ids are carried onto `
    + `the new slides by title.\n\nReplace the deck?`)) return;

  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  form.append("description", $("lib-desc").value || "");
  form.append("overwrite", exists ? "true" : "false");
  if (S.stage.pdf) form.append("pdf", S.stage.pdf);
  flash(`Importing ${file.name}…`, "ok");
  try {
    const r = await api("/api/libraries", { method: "POST", body: form });
    flash(`Imported ${r.imported.slides} slides as "${name}"`, "ok");
    // Replacing a deck is the one import where what happened to the EXISTING
    // work is the interesting part. Said in the order an author has to act on
    // it: what was kept, what arrived, and what is now broken.
    const c = r.imported.carried;
    if (c) {
      flash(`Kept ${c.kept} of ${c.of} block ids — your rules came with them`, "ok");
      if (c.added.length)
        flash(`${c.added.length} new slide(s), in no rule yet: `
          + c.added.map(a => a.title || a.id).join(", "), "ok");
      (r.imported.dangling || []).forEach(msg => flash(msg, "bad"));
      if (c.gone.length && !(r.imported.dangling || []).length)
        flash(`${c.gone.length} slide(s) gone, and no rule referred to them`, "ok");
    }
    // A refused PDF is not a failed import, but it is not nothing either: the
    // viewer will fall back and nobody would know why unless it is said here.
    const p = r.imported.preview || {};
    if (!p.pdf) flash(`No preview PDF: ${p.why || "none was uploaded"}`, "bad");
    S.stage = { pptx: null, pdf: null };
    renderStaged();
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
      : clash ? `"${v}" already exists. Uploading replaces its deck and keeps its rules.`
        : `Saves to templates/${v}/ — its own library.pptx, rules.json and bindings.`;
};

// ------------------------------------------------------------- 2. rules
function renderRules() { renderPool(); renderDeck(); renderTest(); }

function renderPool() {
  const q = ($("pool-search").value || "").toLowerCase();
  const inDeck = new Set(S.deck.map(r => r.id));
  const rest = (S.inspect ? S.inspect.blocks : [])
    .filter(b => !inDeck.has(b.id))
    // "47" and "s47" both find slide 47: an author searching here has a slide
    // number in front of them at least as often as a title.
    .filter(b => !q || b.id.includes(q) || b.title.toLowerCase().includes(q)
                 || String(b.number) === q.replace(/^s/, ""));
  $("pool-count").textContent = `(${rest.length})`;
  $("pool").innerHTML = rest.map(b => `
    <div class="pool-item" draggable="true" data-id="${esc(b.id)}">
      ${srcHTML(b.id)}
      ${thumb(b.id, "xs")}
      <div class="meta"><div class="t" title="${esc(b.title)}">${esc(b.title)}</div>
        <div class="id">${esc(b.id)}</div></div>
      <button class="add" title="Add to the end of the deck" data-add="${esc(b.id)}">+</button>
    </div>`).join("")
    || `<p style="color:var(--muted);font-size:13px">Every slide is in the deck.</p>`;
}

const LEAF_OPS = ["eq", "ne", "in", "not_in", "exists"];
const isLeaf = (c) => !!c && typeof c === "object" && !Array.isArray(c)
  && typeof c.field === "string" && LEAF_OPS.some(k => k in c);

/** A condition as the editor thinks of it: one join and a flat list of clauses.
 *
 *  rules.py allows shapes these controls cannot draw - a group inside a group,
 *  and `not`. Those come back `ok: false` so the editor can refuse them. It
 *  used to do the opposite: `{any: [...]}` has no `field`, so the editor found
 *  none, drew "always", and wrote null over the author's rule the moment
 *  anybody pressed Apply. An editor that cannot show a rule must not be able
 *  to destroy it either.
 */
function condParts(when) {
  if (!when || when === "always") return { join: "all", parts: [], ok: true };
  if (isLeaf(when)) return { join: "all", parts: [when], ok: true };
  for (const join of ["all", "any"]) {
    if (Array.isArray(when[join]) && Object.keys(when).length === 1
      && when[join].length && when[join].every(isLeaf))
      return { join, parts: when[join].slice(), ok: true };
  }
  return { join: "all", parts: [], ok: false };
}

/** The inverse. One clause stays a bare condition rather than a group of one:
 *  rules.json is meant to be read, and {"all": [x]} is noise. Clauses with no
 *  field are dropped - that is what an unfilled row in the editor is. */
function partsCond(join, parts) {
  const real = (parts || []).filter(p => p && p.field);
  if (!real.length) return null;
  return real.length === 1 ? real[0] : { [join]: real };
}

function leafText(w) {
  const f = label(w.field);
  const show = (v) => isDate(w.field) ? humanDate(v) : JSON.stringify(v);
  if ("exists" in w) return { lab: f, op: "is", val: "answered" };
  if ("eq" in w) return { lab: f, op: "is", val: show(w.eq) };
  if ("ne" in w) return { lab: f, op: "is not", val: show(w.ne) };
  if ("in" in w) return { lab: f, op: "is one of", val: w.in.join(", ") };
  if ("not_in" in w) return { lab: f, op: "is not one of", val: w.not_in.join(", ") };
  return { lab: f, op: "", val: "" };
}

/** The chip on the row. Shows the first clause and counts the rest, with the
 *  word that joins them - "or 2 more" and "and 2 more" are different rules and
 *  the row has to say which. */
function condText(when) {
  if (!when) return { lab: "always", op: "", val: "", more: "" };
  const { join, parts, ok } = condParts(when);
  if (!ok) return { lab: "advanced rule", op: "", val: "", more: "in rules.json" };
  if (!parts.length) return { lab: "always", op: "", val: "", more: "" };
  const rest = parts.length - 1;
  return Object.assign(leafText(parts[0]), {
    more: rest ? `${join === "any" ? "or" : "and"} ${rest} more` : "",
  });
}

const condPlain = (w) => {
  if (!w) return "always";
  const { join, parts, ok } = condParts(w);
  if (!ok) return JSON.stringify(w);
  const t = parts.map(p => `${p.field} ${leafText(p).op} ${leafText(p).val}`.trim());
  return t.join(join === "any" ? " or " : " and ") || "always";
};

/** The same comparison rules.py makes, so a greyed row here means an absent
 *  slide there. It is only a preview: /select is what decides the build. */
function holds(when) {
  if (!when || when === "always") return true;
  if (when === "never") return false;
  // Groups first, and exactly as rules.py orders them. A condition this did
  // not understand used to fall through to `return true`, so an {any: [...]}
  // showed as in the deck whatever the answers were - the one thing this
  // function exists to never do.
  if (Array.isArray(when.all)) return when.all.every(c => holds(c));
  if (Array.isArray(when.any)) return when.any.some(c => holds(c));
  if ("not" in when) return !holds(when.not);
  const v = S.answers[when.field];
  const s = (x) => String(x ?? "").replace(/\s+/g, " ").trim().toLowerCase();
  if ("exists" in when) return (s(v) !== "") === !!when.exists;
  if ("eq" in when) return s(v) === s(when.eq);
  if ("ne" in when) return s(v) !== s(when.ne);
  if ("in" in when) return when.in.some(x => s(x) === s(v));
  if ("not_in" in when) return !when.not_in.some(x => s(x) === s(v));
  return true;
}

/** Move `id` to 1-based list position `pos`. Pure: returns a new deck.
 *
 *  Remove first, then insert at pos-1. That is right in both directions: the
 *  removal shifts everything after it up by one, which is exactly what makes
 *  "move to 20" land at 20 when travelling down as well as up.
 */
function moveRow(deck, id, pos) {
  const from = deck.findIndex(r => r.id === id);
  if (from < 0) return deck;
  const target = Math.max(1, Math.min(Math.round(pos) || 0, deck.length));
  const rest = deck.filter(r => r.id !== id);
  rest.splice(target - 1, 0, deck[from]);
  return rest;
}

/** The "move to" bar, opened by clicking a row.
 *
 *  Dragging is fine for ten slides and miserable for a hundred and twenty-eight:
 *  the target scrolls out of sight long before the pointer arrives.
 *
 *  The position it takes is the one in the row's left column - the same
 *  number, deliberately. The other number on the row, `s47`, is the slide's
 *  place in library.pptx and is never what this moves it to; it is repeated
 *  here so an author working against Templafy can see they opened the bar on
 *  the slide they meant.
 */
function moveBarHTML(row, index) {
  return `<div class="moverow" data-moving="${esc(row.id)}">
    <span class="m">${esc(titleOf(row.id))}
      <span class="src">s${slideNo(row.id) || "?"}</span>
      is <b>#${index + 1}</b> of ${S.deck.length}</span>
    <label class="movelab">Move to
      <input type="number" id="move-to" min="1" max="${S.deck.length}"
             value="${index + 1}" data-move-input></label>
    <button class="btn primary sm" data-move-go="${esc(row.id)}">Move</button>
    <button class="btn sm" data-move-cancel="1">Cancel</button>
    <span class="fld-note">Enter moves &middot; Esc closes</span>
  </div>`;
}

function renderDeck() {
  let n = 0, off = 0;
  $("deck").innerHTML = S.deck.map((row, index) => {
    const on = holds(row.when);
    on ? n++ : off++;               // for the tally, not for the row number
    const c = condText(row.when);
    return `<div class="row ${on ? "" : "excluded"} ${S.moving === row.id ? "moving" : ""}"
        draggable="true" data-id="${esc(row.id)}" data-move="${esc(row.id)}">
      <span class="grip">&#8942;&#8942;</span>
      <span class="num" title="Position ${index + 1} of ${S.deck.length} — click to move it">${index + 1}</span>
      ${srcHTML(row.id)}
      ${thumb(row.id, "sm")}
      <div class="namecell"><div class="t" title="${esc(titleOf(row.id))}">${esc(titleOf(row.id))}</div>
        <div class="id">${esc(row.id)}</div></div>
      <button class="chip ${row.when ? "cond" : ""}" data-edit="${esc(row.id)}"
        title="${esc(condPlain(row.when))}">
        <span class="lab">${esc(c.lab)}</span>
        ${c.op ? `<span class="op">${esc(c.op)}</span>` : ""}
        ${c.val ? `<span class="val">${esc(c.val)}</span>` : ""}
        ${c.more ? `<span class="more">${esc(c.more)}</span>` : ""}
      </button>
      <span class="status">${on ? "" : "not in deck"}</span>
      <button class="del" data-del="${esc(row.id)}" title="Remove from the deck">&times;</button>
    </div>`
      + (S.moving === row.id ? moveBarHTML(row, index) : "")
      + (S.editing === row.id ? editorHTML(row) : "");
  }).join("");
  $("deck-count").textContent = `(${S.deck.length} slides, ${n} for these answers)`;
  $("tally").textContent = n;
  $("tally-off").textContent = off ? ` · ${off} excluded` : "";
  renderSaveState();
  focusMoveInput();
}

/** Put the cursor in the box, selected, so a position can just be typed. */
function focusMoveInput() {
  const box = $("move-to");
  if (!box || !S.moving) return;
  box.focus();
  if (box.select) box.select();
}

/** Apply the typed position, then show where the slide landed.
 *
 *  After a move of any distance the row is off screen, so scrolling to it and
 *  marking it is the only way to see that the right slide went to the right
 *  place.
 */
function applyMove(id) {
  const box = $("move-to");
  const pos = box ? Number(box.value) : NaN;
  if (!pos || pos < 1 || pos > S.deck.length) {
    flash(`Give a position between 1 and ${S.deck.length}`, "bad");
    return;
  }
  const before = S.deck.findIndex(r => r.id === id);
  S.deck = moveRow(S.deck, id, pos);
  S.moving = null;
  const landed = S.deck.findIndex(r => r.id === id);
  if (landed !== before) S.dirty = true;
  renderDeck(); renderTest(); renderBuildResult();
  // Found by comparing dataset, not a selector: a block id is author-chosen
  // and CSS.escape is not somewhere to learn that the hard way.
  const el = [...document.querySelectorAll(".row")].find(r => r.dataset.id === id);
  if (el) {
    if (el.scrollIntoView) el.scrollIntoView({ block: "center" });
    el.classList.add("just-moved");
    setTimeout(() => el.classList.remove("just-moved"), 1600);
  }
  flash(`${titleOf(id)} is now #${landed + 1}`, "ok");
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

/** One clause: field, operator, value, and the word joining it to the one
 *  above. The joiner is spelled out on every row after the first, because the
 *  difference between "and" and "or" is the whole rule and a dropdown at the
 *  top is too far away to read while looking at the clauses. */
function condRowHTML(p, i, n, join) {
  const field = p.field || "";
  const op = "exists" in p ? "exists"
    : (["eq", "ne", "in", "not_in"].find(k => k in p) || "eq");
  return `<div class="cond-row" data-p="${i}">
    <span class="joiner">${i === 0 ? "" : (join === "any" ? "or" : "and")}</span>
    <select data-f style="max-width:320px">
      <option value=""${field ? "" : " selected"}>${n > 1
        ? "— pick a field —" : "always — no condition"}</option>
      ${Object.keys(S.fields).map(f => `<option value="${esc(f)}"${f === field ? " selected" : ""}>${esc(label(f))}${S.fields[f].varies === false ? " (never varies)"
        : S.fields[f].missing ? ` (in ${S.fields[f].seen} of ${S.fields[f].of})` : ""}</option>`).join("")}
    </select>
    ${field ? `<select data-o>${OPERATORS.map(([k, l]) =>
      `<option value="${k}"${k === op ? " selected" : ""}>${l}</option>`).join("")}</select>` : ""}
    ${field ? valueControl(field, op, p[op]) : ""}
    ${n > 1 ? `<button class="del" data-delcond="${i}"
      title="Remove this condition">&times;</button>` : ""}
  </div>`;
}

/** The editor's controls -> {join, parts}.
 *
 *  Read back out of the DOM rather than kept in a model beside it. A value
 *  typed into a box and not yet applied exists only in the DOM, so a second
 *  copy of this state is a second thing to get wrong - and getting it wrong
 *  means silently writing a different rule than the one on screen.
 */
/** The join the editor should show.
 *
 *  Deliberately NOT read back out of row.when. Until a second clause has a
 *  field, `partsCond` collapses the condition to a bare clause - correctly,
 *  because {"any": [x]} is not a thing worth writing - and the group is the
 *  only place the author's choice of "or" was being kept. So picking "any one
 *  of these" on a half-filled second row snapped straight back to "all", and
 *  or was unreachable: by the time both clauses were filled the rule had
 *  already been rebuilt as "all". The choice belongs to the editing session,
 *  not to the rule.
 */
function editorJoin(when) {
  return S.condJoin || condParts(when).join;
}

function readEditor(ed) {
  const joinEl = ed.querySelector("[data-join]");
  const parts = [...ed.querySelectorAll(".cond-row")].map(el => {
    const field = (el.querySelector("[data-f]") || {}).value || "";
    if (!field) return {};
    const op = (el.querySelector("[data-o]") || {}).value || "eq";
    const v = el.querySelector("[data-v]");
    if (op === "exists") return { field, exists: true };
    if (op === "in" || op === "not_in")
      return { field, [op]: String(v ? v.value : "").split(",")
        .map(x => x.trim()).filter(Boolean) };
    return { field, [op]: v ? readValue(v) : "" };
  });
  return { join: joinEl ? joinEl.value : (S.condJoin || "all"), parts };
}

function editorHTML(row) {
  const { parts, ok } = condParts(row.when);
  const join = editorJoin(row.when);
  const names = Object.keys(S.fields);
  if (!ok)
    return `<div class="editor">
      <div class="note warn"><b>This rule is more than these controls can
        draw.</b> It nests a group inside a group, or uses <code>not</code> —
        both of which rules.json allows and this editor does not. Editing it
        here would write something simpler over it, so it is left alone.
        Change it in <code>rules.json</code>.</div>
      <div class="full">${esc(JSON.stringify(row.when))}</div>
      <div class="acts"><button class="btn" data-cancel="1">Close</button></div>
    </div>`;
  const shown = parts.slice();
  while (shown.length < Math.max(1, S.condRows || 1)) shown.push({});
  const field = (shown[0] || {}).field || "";
  const spec = S.fields[field];
  if (!names.length)
    return `<div class="editor"><div class="note warn">No answer fields yet.
      Upload a payload on the Build screen first — conditions are chosen from
      the fields the payloads actually contain, never typed.</div>
      <div class="acts"><button class="btn" data-cancel="1">Close</button></div></div>`;
  return `<div class="editor">
    <div class="line">
      <b style="font-size:12.5px">Include this slide when</b>
      ${shown.length > 1 ? `<select data-join>
        <option value="all"${join === "all" ? " selected" : ""}>all of these are true</option>
        <option value="any"${join === "any" ? " selected" : ""}>any one of these is true</option>
      </select>` : ""}
    </div>
    ${shown.map((p, i) => condRowHTML(p, i, shown.length, join)).join("")}
    <div class="line">
      <button class="btn sm" data-addcond="1">+ Add a condition</button>
      ${shown.length > 1 ? `<span class="fld-note">${join === "any"
        ? "any one of them is enough" : "every one of them must hold"}</span>` : ""}
    </div>
    ${field ? `<div class="full" title="the exact field name written to rules.json">${esc(field)}</div>` : ""}
    ${spec && spec.varies === false
      ? `<div class="note warn">Every payload answers this the same way
          (${esc(JSON.stringify(spec.eg))}), so a condition on it is always true
          or always false. Pick a field that varies.</div>`
      : spec && spec.missing
      ? `<div class="note">Answered in ${spec.seen} of ${spec.of} answer sets and
          absent from ${spec.missing}. <b>is answered</b> matches exactly the
          ${spec.seen} that carry it — a value test only matches the ones that
          also hold that value.</div>`
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

// One question, one control. Both forms render through this: the one a
// colleague fills in to build a deck, and the one an author fills in to make
// an example answer set. Two implementations would drift, and the drift would
// show as a field that behaves differently depending which screen you are on.
function question(f, spec, cur, attr, idp) {
  spec = spec || {};
  const p = idp || "q";
  if (spec.kind === "bool")
    return `<label class="q bool"><input type="checkbox" ${attr}="${esc(f)}"${cur === true ? " checked" : ""}>
          <span class="qt">${esc(label(f))}</span></label>`;
  let ctl;
  if (spec.kind === "date")
    ctl = `<input type="date" ${attr}="${esc(f)}" data-date value="${esc(toISO(cur))}">`;
  else if ((spec.values || []).length && spec.values.length <= 12)
    ctl = `<select ${attr}="${esc(f)}"><option value=""></option>${spec.values.map(v =>
      `<option${String(v) === String(cur) ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  else if ((spec.values || []).length)
    ctl = `<input type="text" ${attr}="${esc(f)}" list="${p}-${esc(f)}" value="${esc(cur ?? "")}"
                 placeholder="${spec.values.length} options">
               <datalist id="${p}-${esc(f)}">${spec.values.map(v => `<option value="${esc(v)}">`).join("")}</datalist>`;
  else
    ctl = `<input type="text" ${attr}="${esc(f)}" value="${esc(cur ?? "")}"
                 placeholder="${esc(spec.eg ?? "")}">`;
  return `<label class="q"><span class="qt" title="${esc(f)}">${esc(label(f))}</span>${ctl}</label>`;
}

// Every field this library will ask somebody for. The catalogue knows the ones
// that have appeared in an answer set; the placeholders know the rest. A
// library that has just been marked up has no answer sets at all, so without
// the second half the form would be empty exactly when it is most needed.
function asked() {
  const out = {};
  Object.keys(S.fields || {}).forEach(f => out[f] = S.fields[f]);
  ((S.inspect && S.inspect.placeholders) || []).forEach(name => {
    const bind = (S.bindings || {})[`{{${name}}}`];
    // A literal or a data source is filled in without asking anybody.
    if (bind && bind.from && bind.from !== "field") return;
    const field = (bind && bind.field) || name;
    if (!out[field]) out[field] = { kind: "text", values: [], eg: "" };
  });
  return out;
}

const QF = {};            // what the author has typed into the form
const QFOFF = new Set();  // questions they have taken off it
const QFADD = new Set();  // and ones they have put on

// Which fields the deck itself needs somebody to answer. Taking one of these
// off the form is allowed - it is the author's form - but it is worth saying
// what it costs, because the placeholder it feeds will come out unfilled.
function neededByDeck() {
  const need = new Set();
  ((S.inspect && S.inspect.placeholders) || []).forEach(name => {
    const bind = (S.bindings || {})[`{{${name}}}`];
    if (bind && bind.from && bind.from !== "field") return;
    need.add((bind && bind.field) || name);
  });
  return need;
}

function renderQuestionForm() {
  // The derived list is a starting point, not a cage: the placeholders say
  // what the deck needs, and the author says what this answer set carries.
  // Those are not always the same thing - a payload that arrives without a
  // key is the ordinary case, not an error.
  const fields = asked();
  QFADD.forEach(f => {
    if (!fields[f]) fields[f] = { kind: "text", values: [], eg: "" };
  });
  const names = Object.keys(fields).filter(f => !QFOFF.has(f)).sort();
  $("qf-count").textContent = names.length ? `(${names.length})` : "";
  $("qf-bar").hidden = !names.length;

  const fresh = names.filter(f => !S.fields[f]);
  const need = neededByDeck();
  const dropped = [...QFOFF].filter(f => need.has(f));
  $("qf-note").innerHTML = !names.length
    ? `<b>Nothing to ask yet</b> Mark some text on the Mark text screen, or
       drop an answer set below, and the questions appear here.`
    : `<b>${names.length} question(s)</b> This is exactly what a colleague is
       asked when they build a deck.` + (fresh.length
      ? ` ${fresh.length} of them ${fresh.length === 1 ? "has" : "have"} never
         been answered &mdash; filling this in is what teaches the tool what
         they look like.` : "");
  $("qf-note").className = "notice " + (dropped.length ? "warn" : "ok");
  if (dropped.length) {
    $("qf-note").innerHTML += `<br><b>${dropped.map(f =>
      esc(label(f))).join(", ")}</b> ${dropped.length === 1 ? "is" : "are"} used
      on a slide. Leaving ${dropped.length === 1 ? "it" : "them"} off is fine
      for this answer set, but the placeholder stays unfilled unless you give
      it a fixed value on the Values screen.`;
  }

  $("qf-form").innerHTML = (names.length
    ? `<div class="formgrid">` + names.map(f =>
      `<div class="qfrow">${question(f, fields[f], QF[f], "data-newans", "qf")}`
      + `<button class="x qf-x" data-qdrop="${esc(f)}"
           title="Do not ask this one" aria-label="Remove ${esc(f)}"
           >&times;</button></div>`).join("") + `</div>`
    : "")
    + `<div class="qfadd">
        <input type="text" id="qf-new" autocomplete="off"
          placeholder="Add another question, e.g. Engagement partner">
        <button class="btn" id="qf-add" type="button">Add</button>
        ${QFOFF.size ? `<span style="flex:1"></span>
          <button class="btn linky" id="qf-back" type="button">Bring back
            ${QFOFF.size} removed</button>` : ""}
      </div>`;
}

function qfAdd() {
  const box = document.getElementById("qf-new");
  const raw = ((box && box.value) || "").trim();
  if (!raw) { if (box) box.focus(); return; }
  const key = raw.replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  if (!key) { box.focus(); return; }
  const flat = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");
  // The same guard as the marking picker: a second `Client` alongside
  // `client` is two questions for one thing, and neither fills reliably.
  const clash = Object.keys(asked()).concat([...QFADD])
    .find(f => flat(f) === flat(key));
  if (clash) {
    const was = QFOFF.delete(clash);
    renderQuestionForm();
    flash(was ? `"${label(clash)}" is back on the form.`
      : `"${label(clash)}" is already on the form.`, was ? "ok" : "warn");
    return;
  }
  QFADD.add(key);
  QFOFF.delete(key);
  renderQuestionForm();
  const again = document.getElementById("qf-new");
  if (again) again.focus();
}

document.addEventListener("click", (ev) => {
  const off = ev.target.closest("[data-qdrop]");
  if (off) {
    QFOFF.add(off.dataset.qdrop);
    QFADD.delete(off.dataset.qdrop);
    delete QF[off.dataset.qdrop];
    renderQuestionForm();
    return;
  }
  if (ev.target.closest("#qf-add")) { qfAdd(); return; }
  if (ev.target.closest("#qf-back")) { QFOFF.clear(); renderQuestionForm(); }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && ev.target && ev.target.id === "qf-new") {
    ev.preventDefault();
    qfAdd();
  }
});

function qfTyped(ev) {
  const t = ev.target;
  if (!t || !t.dataset || t.dataset.newans === undefined) return;
  QF[t.dataset.newans] = t.type === "checkbox" ? t.checked
    : (t.dataset.date !== undefined ? fromISO(t.value) : t.value);
}
document.addEventListener("input", qfTyped);
document.addEventListener("change", qfTyped);

$("qf-save").onclick = async () => {
  const name = ($("qf-name").value || "").trim();
  if (!name) { $("qf-name").focus(); return; }
  // Blanks are dropped rather than saved as "". A field nobody answered has
  // not been seen, and recording it as empty would put a blank option into
  // every condition dropdown in the tool.
  const answers = {};
  Object.keys(QF).forEach(f => {
    const v = QF[f];
    if (v !== "" && v !== null && v !== undefined) answers[f] = v;
  });
  if (!Object.keys(answers).length) {
    flash("Nothing filled in yet.", "warn"); return;
  }
  try {
    const cat = await sendJSON(`/api/libraries/${S.lib}/payloads`,
      { name, answers }, "POST");
    S.fields = cat.fields; S.presets = cat.payloads; S.shared = cat.shared;
    Object.keys(QF).forEach(f => delete QF[f]);
    $("qf-name").value = "";
    fillPresets();
    renderQuestions(); renderRules(); renderMapping(); renderBuild();
    flash(`${cat.replaced ? "Replaced" : "Saved"} ${name} — `
      + `${Object.keys(answers).length} answer(s), `
      + `${Object.keys(S.fields).length} field(s) now`, "ok");
  } catch (err) { flash(err.message, "bad"); }
};

function renderQuestions() {
  renderQuestionForm();
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
        : s.missing
        ? `<span class="pill ok">usable</span>
           <span class="preview">in ${s.seen} of ${s.of}</span>`
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
    ? `<div class="formgrid">` + names.map(f =>
      question(f, S.fields[f], S.answers[f], "data-ans")).join("") + `</div>`
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
  syncBuildLayout();
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

/** The build screen has two shapes: before a deck, and with one. */
function syncBuildLayout() {
  const cols = $("build-cols");
  if (cols) cols.classList.toggle("with-deck", !$("viewer-panel").hidden);
}

function renderViewer() {
  const panel = $("viewer-panel");
  if (!S.slides || !S.slides.length) { panel.hidden = true; syncBuildLayout(); return; }
  panel.hidden = false;
  syncBuildLayout();
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
  syncBuildLayout();
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
    // Name the renderer. The deployment will not have PowerPoint, so "which one
    // drew this" is the difference between a preview that holds and one that
    // changes underneath you later.
    const NAMED = { libreoffice: "LibreOffice", powerpoint: "PowerPoint",
                    library: "your library" };
    note.className = r.engine === "svg" ? "hint warn-text" : "hint";
    note.innerHTML = r.engine === "svg"
      ? `approximation, not the real deck &mdash; ${esc(r.why || "no renderer is "
        + "available here")}. Check the downloaded file before trusting how this
         looks.`
      : `rendered from ${NAMED[r.engine] || esc(r.engine)}
         &middot; arrow keys to move`;
    // The library preview is cut from pages that predate substitution, so the
    // cover reads {{ClientName}}. Say it plainly and every time: someone who
    // thinks their deck shipped that way will go looking for a bug that is not
    // there, and someone who assumes the values are fine has not checked them.
    const dis = $("viewer-note");
    dis.hidden = r.engine !== "library";
    dis.innerHTML = `<b>*</b> Placeholders show unfilled here. These are your
      library's own slides, so this confirms <b>which slides and in what
      order</b> &mdash; the filled values are in the downloaded .pptx, and on
      the Values screen.`;
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

/** "Slide 47 in Templafy - where is it here?"
 *
 *  Answers with the row, not with a number: a position typed back at an author
 *  is one more thing for them to go and find. A slide that is not in the deck
 *  is still answered, from the pool, because that is the case where the
 *  question is worth asking - it is how you notice the order is missing one.
 */
function gotoSlide() {
  const want = Number(($("goto-slide").value || "").trim());
  const note = $("goto-note");
  const blocks = (S.inspect && S.inspect.blocks) || [];
  const hit = blocks.find(b => b.number === want);
  document.querySelectorAll(".found").forEach(el => el.classList.remove("found"));
  if (!want || !hit) {
    note.innerHTML = blocks.length
      ? `This library has slides 1 to ${blocks.length}.`
      : "No library open.";
    return;
  }
  const inDeck = S.deck.some(r => r.id === hit.id);
  const sel = inDeck ? ".row" : ".pool-item";
  const el = [...document.querySelectorAll(sel)].find(r => r.dataset.id === hit.id);
  if (el) {
    if (el.scrollIntoView) el.scrollIntoView({ block: "center" });
    el.classList.add("found");
    setTimeout(() => el.classList.remove("found"), 1600);
  }
  note.innerHTML = inDeck
    ? `s${hit.number} &middot; ${esc(hit.title || hit.id)} &mdash; #${
        S.deck.findIndex(r => r.id === hit.id) + 1} in this list`
    : `s${hit.number} &middot; ${esc(hit.title || hit.id)} &mdash;
       <b>not in the deck</b>, it is in the slides on the left`;
}

// ----------------------------------------------------------------- events
$("pool-search").oninput = renderPool;
$("goto-go").onclick = gotoSlide;
$("goto-slide").onkeydown = (e) => { if (e.key === "Enter") gotoSlide(); };

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-add],[data-del],[data-edit],[data-cancel],[data-apply],[data-addcond],[data-delcond],[data-restore],[data-drop],[data-move],[data-move-go],[data-move-cancel],[data-move-input]");
  if (!t) return;
  // Move-to first: its controls sit inside the bar, and the bar sits next to
  // a row that also answers to [data-move].
  if (t.dataset.moveInput !== undefined) return;          // typing, not a click
  if (t.dataset.moveGo !== undefined) { applyMove(t.dataset.moveGo); return; }
  if (t.dataset.moveCancel !== undefined) { S.moving = null; renderDeck(); return; }
  if (t.dataset.move !== undefined) {
    S.moving = S.moving === t.dataset.move ? null : t.dataset.move;
    renderDeck();
    return;
  }
  if (t.dataset.restore !== undefined) { restore(t.dataset.restore); return; }
  if (t.dataset.drop !== undefined) { dropSample(t.dataset.drop); return; }
  if (t.dataset.add !== undefined) { S.deck.push({ id: t.dataset.add, when: null }); S.dirty = true; }
  else if (t.dataset.del !== undefined) { S.deck = S.deck.filter(r => r.id !== t.dataset.del); S.dirty = true; }
  else if (t.dataset.edit !== undefined) {
    S.editing = S.editing === t.dataset.edit ? null : t.dataset.edit;
    // How many clause rows to draw. It cannot come from the condition alone:
    // a row an author has added but not yet filled in holds no clause, so
    // rebuilding from `when` would make it vanish as they reached for it.
    const opening = S.deck.find(r => r.id === S.editing);
    const was = opening ? condParts(opening.when) : { parts: [], join: "all" };
    S.condRows = was.parts.length || 1;
    S.condJoin = was.join;
    renderDeck(); return;
  } else if (t.dataset.cancel !== undefined) { S.editing = null; renderDeck(); return; }
  else if (t.dataset.apply !== undefined) {
    const ed = t.closest(".editor");
    const row = S.deck.find(r => r.id === t.dataset.apply);
    const { join, parts } = readEditor(ed);
    row.when = partsCond(join, parts);
    S.editing = null; S.dirty = true;
  }
  else if (t.dataset.addcond !== undefined) {
    const ed = t.closest(".editor");
    const row = S.deck.find(r => r.id === S.editing);
    const { join, parts } = readEditor(ed);
    row.when = partsCond(join, parts);
    S.condRows = parts.length + 1;
    renderDeck(); return;
  }
  else if (t.dataset.delcond !== undefined) {
    const ed = t.closest(".editor");
    const row = S.deck.find(r => r.id === S.editing);
    const { join, parts } = readEditor(ed);
    parts.splice(Number(t.dataset.delcond), 1);
    row.when = partsCond(join, parts);
    S.condRows = Math.max(1, parts.length);
    S.dirty = true;
    renderDeck(); return;
  }
  renderPool(); renderDeck(); renderTest(); renderBuildResult();
});

document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.matches("[data-f],[data-o],[data-join]")) {
    const ed = t.closest(".editor");
    const row = S.deck.find(r => r.id === S.editing);
    if (t.matches("[data-join]")) S.condJoin = t.value;
    const { join, parts } = readEditor(ed);
    // A new field or a new operator makes the old value meaningless - a date
    // left over from a text field, or a list left over from `is one of`. Only
    // the clause that changed is cleared; the others are somebody's work.
    const rowEl = t.closest(".cond-row");
    if (rowEl) {
      const i = Number(rowEl.dataset.p);
      const field = (rowEl.querySelector("[data-f]") || {}).value || "";
      const op = (rowEl.querySelector("[data-o]") || {}).value || "eq";
      parts[i] = !field ? {}
        : (op === "exists" ? { field, exists: true } : { field, [op]: "" });
    }
    row.when = partsCond(join, parts);
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

document.addEventListener("keydown", (e) => {
  if (!S.moving || !e.target || e.target.id !== "move-to") return;
  if (e.key === "Enter") { e.preventDefault(); applyMove(S.moving); }
  if (e.key === "Escape") { e.preventDefault(); S.moving = null; renderDeck(); }
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
  DRAG = null; S.dirty = true; S.moving = null;
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

// ============================================================ MARK TEXT
//
// Where an author says which words change for each client, without typing
// `{{ }}` or inventing a name twice.
//
// The picture is the real rendered page out of the library PDF. The boxes over
// it come from the OOXML and are exact, so a click lands on the shape the
// author meant. Text is re-flowed only inside a shape they have opened, and
// only while it is open: `engine/svg.py` lays text out approximately, which is
// fine to look at and wrong to click.
//
// Nothing is written anywhere until Save. Marks live here, in the browser.

const MK = {
  index: 1, map: null, marks: [], open: null, sel: null,
  showValues: true, dragging: false,
};

const mkKey = (s) => [s.kind, s.id, s.row ?? "", s.col ?? ""].join(":");
const mkNorm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

function mkTokens(text) {
  const out = [], re = /\s+|[^\s]+/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push({ t: m[0], at: m.index, space: /^\s/.test(m[0]) });
  }
  return out;
}

const mkHere = () => MK.marks.filter(m => m.slide === MK.index);
const mkUses = (name) => MK.marks.filter(m => m.name === name).length;

function mkNames() {
  // Everything a placeholder could be called here: fields the payloads know
  // about, placeholders already in the library, and anything marked in this
  // session. One list, so the same thing cannot be invented twice.
  const seen = {};
  Object.keys(S.fields || {}).forEach(f => seen[f] = { name: f, from: "field" });
  Object.keys(S.bindings || {}).forEach(k => {
    const n = k.replace(/[{}]/g, "").trim();
    if (n) seen[n] = seen[n] || { name: n, from: "existing" };
  });
  MK.marks.forEach(m => seen[m.name] = seen[m.name] || { name: m.name, from: "new" });
  return Object.values(seen).sort((a, b) => a.name.localeCompare(b.name));
}

function mkSample(name) {
  const f = (S.fields || {})[name];
  const vals = (f && (f.values || f.seen)) || [];
  return vals.length ? String(vals[0]) : "";
}

// --------------------------------------------------------------- drawing
async function renderMark() {
  if (!S.lib) { $("mk-stage").innerHTML = ""; return; }
  const count = (S.inspect && S.inspect.blocks.length) || 0;
  if (MK.index > count) MK.index = 1;
  $("mk-count").textContent = count ? `Slide ${MK.index} of ${count}` : "";
  try {
    MK.map = await getJSON(`/api/libraries/${S.lib}/slides/${MK.index}/text`);
  } catch (err) { flash(err.message, "bad"); return; }
  $("mk-title").textContent = MK.map.title || MK.map.block || "";
  MK.open = null; MK.sel = null;
  mkDrawStage();
  mkRail();
}

function mkDrawStage() {
  const m = MK.map;
  if (!m) return;
  const pct = (v, total) => (100 * v / total).toFixed(4) + "%";
  const boxes = m.shapes.map((s, i) => {
    const [x, y, cx, cy] = s.box;
    const mine = mkHere().filter(k => k.key === mkKey(s));
    const open = MK.open === i;
    return `<div class="mk-box${mine.length ? " has" : ""}${open ? " open" : ""}"
      data-i="${i}" style="left:${pct(x, m.width)};top:${pct(y, m.height)};
      width:${pct(cx, m.width)};height:${pct(cy, m.height)}"
      title="${esc(s.name)}">${open ? mkShapeHTML(s, i)
        : (mine.length ? `<span class="mk-badge">${mine.length}</span>` : "")}</div>`;
  }).join("");

  $("mk-stage").innerHTML =
    `<img id="mk-img" alt="Slide ${MK.index}"
       src="/api/libraries/${S.lib}/slides/${MK.index}/png">
     <div class="mk-layer">${boxes}</div>` +
    (m.unreachable && m.unreachable.length
      ? `<div class="mk-gap">${m.unreachable.length} thing(s) here cannot be
         marked yet: ${esc(m.unreachable.map(u => u.why).join("; "))}</div>` : "");
  $("mk-img").onerror = () => {
    $("mk-stage").insertAdjacentHTML("afterbegin",
      `<div class="mk-nopdf">This library has no PDF, so there is no true
       picture of the slide to mark up. The boxes below are still exact.</div>`);
  };
}

function mkShapeHTML(shape, i) {
  return shape.paragraphs.map(p => {
    const mine = mkHere().filter(k => k.key === mkKey(shape) && k.para === p.index)
      .sort((a, b) => a.from - b.from);
    const toks = mkTokens(p.text);
    let out = "", n = 0;
    while (n < toks.length) {
      const tok = toks[n];
      const mark = mine.find(k => k.from === tok.at);
      if (mark) {
        const shown = MK.showValues && mark.sample ? mark.sample : mark.name;
        out += `<span class="mk-fx" data-mark="${mark.id}"
                 data-tag="${esc(mark.name)}">${esc(shown)}</span>`;
        while (n < toks.length && toks[n].at < mark.to) n++;
        continue;
      }
      if (tok.space) { out += esc(tok.t); n++; continue; }
      const on = MK.sel && MK.sel.shape === i && MK.sel.para === p.index
        && n >= MK.sel.from && n <= MK.sel.to;
      out += `<span class="mk-w${on ? " sel" : ""}" data-w="${n}"
               data-para="${p.index}">${esc(tok.t)}</span>`;
      n++;
    }
    return `<div class="mk-p" data-para="${p.index}">${out}</div>`;
  }).join("");
}

// ------------------------------------------------------------- selection
function mkSelText() {
  if (!MK.sel) return "";
  const p = MK.map.shapes[MK.sel.shape].paragraphs
    .find(x => x.index === MK.sel.para);
  const toks = mkTokens(p.text);
  return p.text.slice(toks[MK.sel.from].at,
    toks[MK.sel.to].at + toks[MK.sel.to].t.length);
}

function mkSelRange() {
  const p = MK.map.shapes[MK.sel.shape].paragraphs
    .find(x => x.index === MK.sel.para);
  const toks = mkTokens(p.text);
  return [toks[MK.sel.from].at, toks[MK.sel.to].at + toks[MK.sel.to].t.length];
}

function mkBar() {
  const bar = $("mk-bar");
  const nodes = $("mk-stage").querySelectorAll(".mk-w.sel");
  if (!MK.sel || !nodes.length) { bar.hidden = true; return; }
  const a = nodes[0].getBoundingClientRect();
  const b = nodes[nodes.length - 1].getBoundingClientRect();
  $("mk-bar-text").textContent = "“" + mkSelText().trim() + "”";
  bar.hidden = false;
  const w = bar.offsetWidth;
  const cx = (Math.min(a.left, b.left) + Math.max(a.right, b.right)) / 2;
  bar.style.left = Math.max(12, Math.min(cx - w / 2, innerWidth - w - 12)) + "px";
  let top = Math.max(a.bottom, b.bottom) + 8;
  if (top + bar.offsetHeight > innerHeight - 12) {
    top = Math.min(a.top, b.top) - bar.offsetHeight - 8;
  }
  bar.style.top = Math.max(12, top) + "px";
}

function mkClearSel() { MK.sel = null; $("mk-bar").hidden = true; mkDrawStage(); }

// ---------------------------------------------------------------- picker
function mkPick(editing) {
  const text = editing ? editing.text : mkSelText();
  // A value the payloads have actually seen is the strongest hint available,
  // and it costs nothing: the catalogue is already loaded.
  const guess = mkNames().find(n => {
    const f = (S.fields || {})[n.name];
    const vals = (f && (f.values || f.seen)) || [];
    return vals.some(v => String(v).trim() === text.trim());
  });

  const row = (n, tag) => {
    const sample = mkSample(n.name);
    const used = mkUses(n.name);
    return `<button class="mk-opt" data-pick="${esc(n.name)}" type="button">
      <span class="t"><b>${esc(label(n.name))}</b><span>${esc(sample || "no example yet")}${
        used ? ` &middot; used ${used}× here` : ""}</span></span>
      ${tag ? `<span class="mk-sug">${tag}</span>` : ""}</button>`;
  };

  const list = () => {
    const names = mkNames();
    let h = "";
    if (guess && !editing) {
      h += `<div class="mk-sep">Looks like</div>${row(guess, "suggested")}`;
      h += `<div class="mk-sep">Or one you already have</div>`;
    } else {
      h += `<div class="mk-sep">One you already have</div>`;
    }
    h += names.filter(n => !guess || editing || n.name !== guess.name)
      .map(n => row(n, n.from === "new" ? "new" : "")).join("")
      || `<p class="mk-empty">No fields yet. Upload a payload on the Questions
          screen, or add one below.</p>`;
    h += `<div class="mk-sep">Not on the list</div>
      <button class="mk-opt" data-new="field" type="button"><span class="t">
        <b>Add a new field&hellip;</b><span>Colleagues will be asked for it on
        the build form</span></span></button>
      <button class="mk-opt" data-new="literal" type="button"><span class="t">
        <b>The same on every proposal</b><span>A fixed value &mdash; nobody is
        asked for it</span></span></button>`;
    return h;
  };

  const scrim = document.createElement("div");
  scrim.className = "mk-scrim";
  scrim.innerHTML = `<div class="mk-sheet" role="dialog" aria-modal="true"
      aria-label="What is this text?">
    <div class="mk-sheet-head"><h3>What is this?</h3>
      <div class="mk-quote">${esc(text)}</div></div>
    <div class="mk-sheet-body" id="mk-body">${list()}</div>
    <div class="mk-sheet-foot">
      ${editing ? `<button class="btn" data-unmark="1">Unmark</button>`
        : `<button class="btn" data-back="1">Change selection</button>`}
      <span style="flex:1"></span>
      <button class="btn" data-close="1">Cancel</button></div></div>`;
  document.body.appendChild(scrim);
  $("mk-bar").hidden = true;

  const close = () => { scrim.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") { close(); mkClearSel(); } };
  document.addEventListener("keydown", onKey);

  function commit(name, binding, sample) {
    if (editing) {
      Object.assign(editing, { name, binding, sample });
    } else {
      const [from, to] = mkSelRange();
      const shape = MK.map.shapes[MK.sel.shape];
      const para = shape.paragraphs.find(x => x.index === MK.sel.para);
      MK.marks.push({
        id: "m" + (Date.now() + MK.marks.length),
        slide: MK.index, key: mkKey(shape), shapeName: shape.name,
        para: MK.sel.para, from, to, text, name, binding, sample,
        // the address the server resolves: which part, which paragraph in it,
        // and the text that paragraph held when it was read. The last one is
        // what catches a library that moved on underneath the browser.
        part: MK.map.part, at: para.at, expect: para.text,
      });
    }
    close(); MK.sel = null; mkDrawStage(); mkRail(); MK.dirty = true;
  }

  scrim.addEventListener("click", (ev) => {
    const pick = ev.target.closest("[data-pick]");
    if (pick) {
      const name = pick.dataset.pick;
      return commit(name, { from: "field", field: name }, mkSample(name));
    }
    const add = ev.target.closest("[data-new]");
    if (add) return mkNewForm(scrim, add.dataset.new, text, commit);
    if (ev.target.closest("[data-unmark]")) {
      MK.marks = MK.marks.filter(m => m !== editing);
      close(); mkDrawStage(); mkRail(); MK.dirty = true; return;
    }
    if (ev.target.closest("[data-back]")) { close(); mkBar(); return; }
    if (ev.target.closest("[data-close]") || ev.target === scrim) {
      close(); mkClearSel();
    }
  });
}

// The duplicate guard. `client` must not become a second `Client`.
function mkNewForm(scrim, kind, selected, commit) {
  const body = scrim.querySelector("#mk-body");
  const literal = kind === "literal";
  body.innerHTML = `<div class="mk-form">
    <label class="fld"><span>What should we call it?</span>
      <input type="text" id="mk-nf" placeholder="LeadPartner" autocomplete="off"></label>
    <div id="mk-clash"></div>
    <label class="fld"><span>${literal ? "The value to use everywhere"
      : "An example, so colleagues know what to type"}</span>
      <input type="text" id="mk-nv" autocomplete="off"></label>
    <button class="btn primary" id="mk-add">Add and use it</button></div>`;
  const name = body.querySelector("#mk-nf"), val = body.querySelector("#mk-nv");
  const clash = body.querySelector("#mk-clash"), add = body.querySelector("#mk-add");
  val.value = literal ? selected.trim() : "";
  val.placeholder = selected.trim().slice(0, 40);
  name.focus();
  let anyway = false;

  const near = () => {
    const n = mkNorm(name.value);
    if (!n) return null;
    return mkNames().find(x => {
      const k = mkNorm(x.name), l = mkNorm(label(x.name));
      return k === n || l === n || k.startsWith(n) || n.startsWith(k);
    }) || null;
  };
  const check = () => {
    const hit = anyway ? null : near();
    if (!hit) { clash.innerHTML = ""; add.disabled = false;
      add.textContent = "Add and use it"; return; }
    const same = mkNorm(hit.name) === mkNorm(name.value);
    clash.innerHTML = `<div class="mk-clash">${same
      ? `<b>You already have this one.</b> It is called <b>${esc(label(hit.name))}</b>.`
      : `<b>Did you mean ${esc(label(hit.name))}?</b> Two names that mean the
         same thing will not fill in properly.`}
      <div class="mk-clash-acts">
        <button class="btn primary" data-use="${esc(hit.name)}">Use
          ${esc(label(hit.name))}</button>
        ${same ? "" : `<button class="btn" data-anyway="1">No, mine is different</button>`}
      </div></div>`;
    add.disabled = same;
    add.textContent = same ? "Already exists" : "Add and use it";
  };
  name.addEventListener("input", () => { anyway = false; check(); });

  body.addEventListener("click", (ev) => {
    const use = ev.target.closest("[data-use]");
    if (use) {
      const n = use.dataset.use;
      return commit(n, { from: "field", field: n }, mkSample(n));
    }
    if (ev.target.closest("[data-anyway]")) { anyway = true; check(); return; }
    if (!ev.target.closest("#mk-add")) return;
    const label_ = name.value.trim();
    if (!label_) { name.focus(); return; }
    const id = label_.replace(/[^A-Za-z0-9_]+/g, "");
    if (!id) { name.focus(); return; }
    commit(id, literal ? { from: "literal", value: val.value }
      : { from: "field", field: id }, val.value.trim());
  });
}

// ------------------------------------------------------------------ rail
function mkRail() {
  const here = mkHere();
  $("mk-here").innerHTML = here.length ? here.map(m =>
    `<div class="mk-row"><span class="mk-dot"></span>
      <span class="t"><b>${esc(label(m.name))}</b>
        <span>${esc(m.text)} &middot; ${esc(m.shapeName)}</span></span>
      <button class="x" data-drop="${m.id}" aria-label="Remove">&times;</button>
     </div>`).join("")
    : `<p class="mk-empty">Nothing marked on this slide. Open a text box and
       drag across the words that change.</p>`;

  const byName = {};
  MK.marks.forEach(m => (byName[m.name] = byName[m.name] || []).push(m));
  const names = Object.keys(byName).sort();
  $("mk-all-count").textContent = names.length ? `(${names.length})` : "";
  $("mk-all").innerHTML = names.length ? names.map(n =>
    `<div class="mk-row"><span class="mk-dot"></span>
      <span class="t"><b>${esc(label(n))}</b>
        <span>${byName[n][0].binding.from === "literal"
          ? "fixed value" : "from the build form"}</span></span>
      <span class="mk-used">${byName[n].length}×</span></div>`).join("")
    : `<p class="mk-empty">Nothing yet.</p>`;

  const slides = new Set(MK.marks.map(m => m.slide));
  $("mk-tally").textContent = MK.marks.length
    ? `${MK.marks.length} marked across ${slides.size} slide(s)`
    : "";
  $("mk-save").disabled = !MK.marks.length;
}

// --------------------------------------------------------------- wiring
document.addEventListener("pointerdown", (ev) => {
  if (!ev.target.closest("#mk-stage")) return;
  const fx = ev.target.closest(".mk-fx");
  if (fx) {
    const m = MK.marks.find(x => x.id === fx.dataset.mark);
    if (m) { MK.sel = null; mkPick(m); }
    return;
  }
  const w = ev.target.closest(".mk-w");
  if (w) {
    ev.preventDefault();
    MK.dragging = true;
    const box = w.closest(".mk-box");
    MK.sel = { shape: +box.dataset.i, para: +w.dataset.para,
      from: +w.dataset.w, to: +w.dataset.w, anchor: +w.dataset.w };
    mkDrawStage(); mkBar();
    return;
  }
  const box = ev.target.closest(".mk-box");
  if (box) {
    const i = +box.dataset.i;
    MK.open = MK.open === i ? null : i;
    MK.sel = null; $("mk-bar").hidden = true;
    mkDrawStage();
  }
});

document.addEventListener("pointerover", (ev) => {
  if (!MK.dragging || !MK.sel) return;
  const w = ev.target.closest(".mk-w");
  if (!w || +w.dataset.para !== MK.sel.para) return;
  const t = +w.dataset.w;
  MK.sel.from = Math.min(MK.sel.anchor, t);
  MK.sel.to = Math.max(MK.sel.anchor, t);
  mkDrawStage(); mkBar();
});

document.addEventListener("pointerup", () => {
  if (MK.dragging) { MK.dragging = false; mkBar(); }
});

document.addEventListener("click", (ev) => {
  const drop = ev.target.closest("#mk-here [data-drop]");
  if (drop) {
    MK.marks = MK.marks.filter(m => m.id !== drop.dataset.drop);
    mkDrawStage(); mkRail(); MK.dirty = true;
  }
});

// --------------------------------------------------------------- toolbar
$("mk-prev").onclick = () => { if (MK.index > 1) { MK.index--; renderMark(); } };
$("mk-next").onclick = () => {
  const n = (S.inspect && S.inspect.blocks.length) || 1;
  if (MK.index < n) { MK.index++; renderMark(); }
};
$("mk-values").onclick = function () {
  MK.showValues = !MK.showValues;
  this.textContent = MK.showValues ? "Showing: example values"
                                   : "Showing: field names";
  mkDrawStage();
};
$("mk-save").onclick = () => mkSave();

// Saving makes a NEW library. The one being marked up is never written to, so
// a mistake here costs nothing that cannot be undone by carrying on with the
// old one - which is why the dialog says so rather than asking for confidence.
function mkSave() {
  if (!MK.marks.length) return;
  const base = String(S.lib || "library").replace(/_v\d+$/, "");
  const taken = new Set((S.libs || []).map(l => l.id));
  let n = 2;
  while (taken.has(base + "_v" + n)) n++;

  const scrim = document.createElement("div");
  scrim.className = "mk-scrim";
  scrim.innerHTML = `<div class="mk-sheet" role="dialog" aria-modal="true"
      aria-label="Save as a new library">
    <div class="mk-sheet-head"><h3>Save as a new library</h3>
      <div class="mk-quote">${MK.marks.length} thing(s) marked across
        ${new Set(MK.marks.map(m => m.slide)).size} slide(s). <b>${esc(S.lib)}</b>
        is not changed &mdash; it stays exactly as it is.</div></div>
    <div class="mk-form">
      <label class="fld"><span>Call the new library</span>
        <input type="text" id="mk-name" value="${esc(base + "_v" + n)}"
          autocomplete="off"></label>
      <label class="fld"><span>What changed (optional)</span>
        <input type="text" id="mk-desc" autocomplete="off"
          placeholder="marked the client name and the fees"></label>
      <p class="mk-empty">Lower case letters, digits and underscores. Your
        rules, bindings and answer sets come with it.</p>
    </div>
    <div class="mk-sheet-foot"><span style="flex:1"></span>
      <button class="btn" data-close="1">Cancel</button>
      <button class="btn primary" id="mk-go-save">Save</button></div></div>`;
  document.body.appendChild(scrim);
  const name = scrim.querySelector("#mk-name");
  name.focus(); name.select();

  scrim.addEventListener("click", async (ev) => {
    if (ev.target.closest("[data-close]") || ev.target === scrim) {
      scrim.remove(); return;
    }
    if (!ev.target.closest("#mk-go-save")) return;
    const go = scrim.querySelector("#mk-go-save");
    go.disabled = true; go.textContent = "Saving\u2026";
    try {
      const out = await sendJSON(`/api/libraries/${S.lib}/mark`, {
        name: name.value.trim(),
        description: scrim.querySelector("#mk-desc").value.trim(),
        marks: MK.marks.map(m => ({
          part: m.part, at: m.at, expect: m.expect,
          start: m.from, end: m.to, name: m.name, binding: m.binding,
        })),
      }, "POST");
      scrim.remove();
      MK.marks = []; MK.dirty = false;
      mkDrawStage(); mkRail();
      await loadLibraries();
      flash(`Saved as ${out.library}. ${out.pdf || ""}`, "ok");
    } catch (err) {
      go.disabled = false; go.textContent = "Save";
      const box = scrim.querySelector(".mk-form");
      box.insertAdjacentHTML("beforeend",
        `<div class="mk-clash"><b>Not saved.</b> ${esc(err.message)}</div>`);
    }
  });
}

// Marks are not part of rules.json, so the guard on that file does not cover
// them: leaving with unsaved marks would lose the session's work silently.
window.addEventListener("beforeunload", (e) => {
  if (MK.dirty && MK.marks.length) { e.preventDefault(); e.returnValue = ""; }
});
$("mk-go").onclick = () => { if (MK.sel) mkPick(null); };
$("mk-x").onclick = mkClearSel;

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
