/* Run the mockup's render functions against a DOM stub that behaves like a DOM.
 *
 * There was an earlier version of this that returned a live object for every
 * id it was asked for. It reported "29 cards, clean" while the real page was
 * blank, because the page referenced an element that had been deleted from the
 * markup: the browser returned null, `.textContent =` threw, and every render
 * after it never ran. A test double more forgiving than reality tests nothing.
 *
 * So this stub returns **null for any id not actually in the HTML**, exactly as
 * getElementById does, and the whole init sequence runs rather than a hand-
 * picked function or two. That is the difference between checking that a
 * renderer produces markup and checking that the page comes up.
 *
 *     node tools/check_ui_render.js            (from ppt_gen_v2/)
 */
const fs = require("fs");
const path = require("path");

const UI = path.join(__dirname, "..", "ui");
const html = fs.readFileSync(path.join(UI, "mockup.html"), "utf8");

// ------------------------------------------------------------------ static
const declared = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const referenced = [...html.matchAll(/getElementById\("([^"]+)"\)/g)].map(m => m[1]);
const dangling = [...new Set(referenced)].filter(id => !declared.has(id));

const problems = [];
if (dangling.length)
  problems.push("getElementById for ids that do not exist: " + dangling.join(", "));

// ------------------------------------------------------------------- stub
const boxes = {};
function element(id) {
  return boxes[id] || (boxes[id] = {
    id, innerHTML: "", textContent: "", hidden: false, value: "", dataset: {},
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false } },
    style: {}, setAttribute() {}, removeAttribute() {}, dispatchEvent() {},
    closest() { return null }, querySelector() { return null },
    querySelectorAll() { return [] }, appendChild() {}, focus() {},
    // real elements have these; a stub missing one reports a page bug that is
    // really a hole in the stub, which wastes exactly the attention this tool
    // is meant to save
    addEventListener() {}, removeEventListener() {}, click() {},
  });
}
const missed = new Set();
global.window = { addEventListener() {}, removeEventListener() {} };
global.localStorage = { getItem: () => null, setItem() {} };
global.Event = function () {};
global.document = {
  getElementById(id) {
    if (declared.has(id)) return element(id);
    missed.add(id);                       // what the browser would do: null
    return null;
  },
  querySelector: () => element("_q"),
  querySelectorAll: () => [],
  addEventListener() {},
  documentElement: { setAttribute() {}, removeAttribute() {} },
  body: { classList: { toggle() {} } },
};

for (const side of ["catalogue.js", "check_samples.js"]) {
  const p = path.join(UI, side);
  if (fs.existsSync(p)) eval(fs.readFileSync(p, "utf8"));
}

// ----------------------------------------------------------------- run it
const script = [...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)]
  .pop()[1];
try {
  eval(script);                            // the whole thing, init included
} catch (err) {
  problems.push("the page threw while initialising: " + err.message);
}
if (missed.size)
  problems.push("looked for missing element(s): " + [...missed].join(", "));

// --------------------------------------------------------------- contents
const EXPECT = [
  ["lib-grid", /class="card"/, "library contact sheet"],
  ["lib-grid", /class="thumb/, "slide thumbnails"],
  ["stats", /class="stat/, "inspect stat row"],
  ["deck", /class="row/, "deck rows"],
  ["pool", /class="pool-item"|Every slide is in the deck/, "library pool"],
  ["map-body", /<tr>/, "placeholder mapping rows"],
  ["answers-form", /class="q/, "build form questions"],
  ["build-list", /class="bl"/, "build result slides"],
  ["check-result", /class="verdict/, "check verdict"],
  ["library", /<option/, "library picker"],
];
const pad = (s, n) => String(s).padEnd(n);
console.log(pad("REGION", 16) + pad("CHARS", 8) + "CONTENT");
for (const [id, re, what] of EXPECT) {
  const h = (boxes[id] || {}).innerHTML || "";
  const ok = re.test(h);
  if (!ok) problems.push(`${id} has no ${what}`);
  console.log(pad(id, 16) + pad(h.length, 8) + (ok ? what : "MISSING: " + what));
}

// a rendered hole is as broken as an empty one, and easier to ship
for (const [id, box] of Object.entries(boxes)) {
  const h = box.innerHTML || "";
  if (/undefined|\[object Object\]|NaN/.test(h))
    problems.push(`${id} rendered undefined/[object Object]/NaN`);
}

console.log("");
if (problems.length) {
  problems.forEach(p => console.log("  ! " + p));
  console.log("\n" + problems.length + " problem(s)");
  process.exit(1);
}
console.log("the page initialises and every region has content");
