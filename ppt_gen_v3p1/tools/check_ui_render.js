/* Boot the real front end against a DOM stub that behaves like a DOM.
 *
 *     node tools/check_ui_render.js          (from ppt_gen_v3/)
 *
 * An earlier version of this returned a live object for every id it was asked
 * for. It reported "29 cards, clean" while the page was blank, because the
 * markup no longer had an element the script still wrote to: the browser
 * returned null, `.textContent =` threw, and every render after it never ran.
 * A test double more forgiving than reality tests nothing.
 *
 * So the stub returns **null for any id not in index.html**, exactly as
 * getElementById does, and app.js is evaluated whole - boot included - rather
 * than a hand-picked function or two.
 *
 * `fetch` is answered from a small set of canned, correctly-shaped responses.
 * That is not a test of the API (tools/check_api.py does that against the real
 * engine); it is a test that the page comes up and renders what it is given.
 */
const fs = require("fs");
const path = require("path");

const WEB = path.join(__dirname, "..", "web");
const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
const js = fs.readFileSync(path.join(WEB, "app.js"), "utf8");

const problems = [];

// ------------------------------------------------------------------ static
// Ids come from the markup *and* from anything the script writes into
// innerHTML - `$("reinspect")` is legitimate when the button was just rendered.
// Counting only index.html would flag that as dangling and train someone to
// ignore this check, which is worse than not having it.
const declared = new Set([...html.matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const rendered = new Set([...js.matchAll(/id="([^"$]+)"/g)].map(m => m[1]));
const referenced = [...js.matchAll(/\$\("([^"]+)"\)|getElementById\("([^"]+)"\)/g)]
  .map(m => m[1] || m[2]);
const dangling = [...new Set(referenced)]
  .filter(id => !declared.has(id) && !rendered.has(id));
if (dangling.length)
  problems.push("the script writes to ids that are not in the markup: "
    + dangling.join(", "));

// -------------------------------------------------------------------- stub
const boxes = {};
const missed = new Set();
function element(id) {
  return boxes[id] || (boxes[id] = {
    id, innerHTML: "", textContent: "", hidden: false, value: "", dataset: {},
    options: [], selectedIndex: 0, files: [], disabled: false,
    classList: {
      _s: new Set(),
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (on ? this._s.add(c) : this._s.delete(c)); },
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    style: {}, setAttribute() { }, removeAttribute() { }, dispatchEvent() { },
    closest() { return null }, querySelector() { return null },
    querySelectorAll() { return [] }, appendChild() { }, focus() { }, click() { },
    insertAdjacentHTML() { },
    // real elements have these; a stub missing one reports a page bug that is
    // really a hole in the stub, which wastes the attention this tool saves
    addEventListener() { }, removeEventListener() { },
  });
}

global.window = { addEventListener() { }, removeEventListener() { } };
global.localStorage = { getItem: () => null, setItem() { } };
global.Event = function () { };
global.FormData = function () { this.append = () => { }; };
global.confirm = () => true;
global.alert = () => { };
global.document = {
  getElementById(id) {
    // `rendered` covers elements the script itself just wrote into innerHTML;
    // in a browser those exist by the time they are looked up.
    if (declared.has(id) || rendered.has(id)) return element(id);
    missed.add(id);
    return null;
  },
  createElement: () => element("_created"),
  querySelector: () => element("_q"),
  querySelectorAll: () => [],
  addEventListener() { },
  documentElement: { setAttribute() { }, removeAttribute() { } },
  body: { classList: { add() { }, remove() { }, toggle() { } }, appendChild() { } },
};

// ------------------------------------------------------------------- fetch
const DEMO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 56"><rect/></svg>';
const CANNED = [
  [/\/fields$/, {
    fields: {
      AuditType: { kind: "choice", values: ["New Audit Client", "Expansion of Services"], eg: "New Audit Client", varies: true },
      DueDate: { kind: "date", values: [], eg: "20261130", varies: true },
      Quality: { kind: "bool", values: [false, true], eg: false, varies: true },
      // answered identically everywhere, so it cannot be a condition - the
      // Questions screen has to say so, and that path needs exercising
      "I agree to comply": { kind: "choice", values: ["Accept"], eg: "Accept", varies: false },
    },
    payloads: [{ label: "p1", answers: { AuditType: "New Audit Client", DueDate: "20261130", Quality: true } }],
    shared: false,
  }],
  [/\/api\/builds\/[0-9a-f]+\/slides/, {
    engine: "library", why: null,
    slides: [{ index: 1, title: "Cover", png: "/api/builds/x/png/1", svg: null, unsupported: [], error: null },
    { index: 2, title: "Fees", png: "/api/builds/x/png/2", svg: null, unsupported: [], error: null }],
  }],
  [/\/api\/libraries$/, [{ id: "demo", name: "Demo", description: "", slides: 3, unnamed: 0, answer_sets: [], real: true }]],
  [/\/thumbs/, [{ id: "cover", title: "Cover", svg: DEMO_SVG, placeholders: [] },
  { id: "scope", title: "Scope", svg: DEMO_SVG, placeholders: ["ClientName"] },
  { id: "fees", title: "Fees", svg: DEMO_SVG, placeholders: ["FeeTotal"] },
  { id: "dropped", title: "Left out", svg: DEMO_SVG, placeholders: [] }]],
  [/\/rules$/, {
    raw: {}, deck: [{ id: "cover", when: null },
    { id: "scope", when: { field: "AuditType", eq: "Expansion of Services" } },
    { id: "fees", when: null }],
    placeholders: { "{{ClientName}}": { from: "field", field: "AuditType" }, "{{FeeTotal}}": null },
  }],
  [/\/select$/, {
    slides: [{ block: "cover", slide: "cover", title: "Cover" },
    { block: "fees", slide: "fees", title: "Fees" }],
    count: 2, unbound: ["FeeTotal"], trace: [],
  }],
  [/\/api\/libraries\/[^/]+$/, {
    library: { id: "demo", name: "Demo", description: "", slides: 3, real: true },
    blocks: [{ id: "cover", title: "Cover", source: "marker", number: 1, part: "slide1.xml", placeholders: [] },
    { id: "scope", title: "Scope", source: "title", number: 2, part: "slide2.xml", placeholders: ["ClientName"] },
    { id: "fees", title: "Fees", source: "marker", number: 3, part: "slide3.xml", placeholders: ["FeeTotal"] },
    { id: "dropped", title: "Left out", source: "map", number: 4, part: "slide4.xml", placeholders: [] }],
    placeholders: ["ClientName", "FeeTotal"], unbound: ["FeeTotal"], unused: [],
    by_source: { marker: 2, title: 1, map: 1 }, drift: [], problems: [], has_rules: true,
  }],
  // The Mark text screen. Two shapes, one of them a table cell, so the check
  // covers both kinds of box the editor has to place.
  [/\/slides\/\d+\/text$/, {
    part: "ppt/slides/slide1.xml", index: 1, block: "cover", title: "Cover",
    width: 12192000, height: 6858000,
    shapes: [
      {
        kind: "shape", id: "4", name: "title", rot: 0,
        box: [914400, 1920240, 10363200, 1600200],
        paragraphs: [{
          index: 0, text: "Globex International Holdings",
          runs: [{ start: 0, end: 29, editable: true }], placeholders: [],
        }],
      },
      {
        kind: "cell", id: "7", row: 1, col: 1, name: "fees r2c2", rot: 0,
        box: [914400, 4000000, 4000000, 400000],
        paragraphs: [{
          index: 0, text: "$248,000",
          runs: [{ start: 0, end: 8, editable: true }], placeholders: [],
        }],
      },
    ],
    unreachable: [],
  }],
];
const calls = [];
global.fetch = async (url) => {
  calls.push(url);
  const hit = CANNED.find(([re]) => re.test(url));
  if (!hit) problems.push("no canned response for " + url);
  return {
    ok: true, statusText: "OK",
    headers: { get: () => "application/json" },
    json: async () => (hit ? hit[1] : {}),
    text: async () => "",
  };
};

// ------------------------------------------------------------------ run it
try {
  // app.js is strict mode, so its scope does not leak into ours. Ask for the
  // one function this file needs to test directly.
  eval(js + "\n;globalThis.__readValue = readValue; globalThis.__formatted = formatted; globalThis.__md = md; globalThis.__viewer = { load: loadBuiltSlides, big, small }; globalThis.__moveRow = moveRow; globalThis.__showTab = showTab; globalThis.__renderMark = renderMark; globalThis.__mkShapeHTML = mkShapeHTML; globalThis.__MK = MK; globalThis.__renderQuestionForm = renderQuestionForm; globalThis.__qfAdd = qfAdd; globalThis.__QFOFF = QFOFF; globalThis.__QFADD = QFADD;");
} catch (err) {
  problems.push("the page threw while initialising: " + err.message);
}

(async function settle() {
  // Wait until the page stops calling the API. Asserting on the first tick
  // tests how fast promises resolve, not whether the page renders.
  let seen = -1, quiet = 0;
  while (quiet < 3) {
    await new Promise(r => setTimeout(r, 20));
    if (calls.length === seen) quiet++; else { quiet = 0; seen = calls.length; }
  }

  if (missed.size)
    problems.push("looked for missing element(s): " + [...missed].join(", "));

  const EXPECT = [
    ["lib-grid", /class="card"/, "library contact sheet"],
    ["stats", /class="stat/, "inspect stat row"],
    ["lib-warn", /stable id/, "the named-by-title warning"],
    ["deck", /class="row/, "deck rows"],
    ["pool", /class="pool-item"|Every slide is in the deck/, "library pool"],
    ["map-body", /<tr>/, "placeholder mapping rows"],
    ["answers-form", /class="q/, "build form questions"],
    ["build-list", /class="bl"/, "build result slides"],
    ["library", /<option/, "library picker"],
    ["q-body", /<tr>/, "the field catalogue"],
    ["q-warn", /never vary|No answer sets/, "the never-varies warning"],
    ["test-body", /data-ans/, "the answers that affect the deck"],
  ];
  const pad = (s, n) => String(s).padEnd(n);
  console.log(pad("REGION", 16) + pad("CHARS", 8) + "CONTENT");
  for (const [id, re, what] of EXPECT) {
    const h = (boxes[id] || {}).innerHTML || "";
    const ok = re.test(h);
    if (!ok) problems.push(`${id} has no ${what}`);
    console.log(pad(id, 16) + pad(h.length, 8) + (ok ? what : "MISSING: " + what));
  }
  for (const [id, box] of Object.entries(boxes)) {
    if (/undefined|\[object Object\]|NaN/.test(box.innerHTML || ""))
      problems.push(`${id} rendered undefined/[object Object]/NaN`);
  }
  // The <option> values are JSON so a boolean survives. Reading them back by
  // guessing left every string wrapped in its own quotes, so a saved condition
  // read `AuditType is "\"New Audit Client\""` and matched nothing - the deck
  // silently lost a slide. Typed input must stay literal, or `null` in a text
  // box becomes a null.
  const cases = [
    [{ dataset: { json: "" }, value: '"New Audit Client"' }, "New Audit Client", "JSON string unwrapped"],
    [{ dataset: { json: "" }, value: "false" }, false, "JSON false stays a boolean"],
    [{ dataset: { json: "" }, value: "true" }, true, "JSON true stays a boolean"],
    [{ dataset: {}, value: "New York" }, "New York", "typed text is literal"],
    [{ dataset: {}, value: "null" }, "null", "typed 'null' is a string, not null"],
    [{ dataset: { date: "" }, value: "2026-11-30" }, "20261130", "date stored as the payload holds it"],
  ];
  if (typeof globalThis.__readValue !== "function")
    problems.push("readValue never got defined - the script did not finish");
  for (const [el, want, why] of (globalThis.__readValue ? cases : [])) {
    const got = globalThis.__readValue(el);
    if (got !== want)
      problems.push(`readValue: ${why} — got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
  console.log("readValue: " + cases.length + " case(s) checked");

  // Moving a slide by typing a position. Dragging row 90 to position 3 is the
  // thing this replaces, so the arithmetic has to be right in BOTH directions:
  // removing the row first shifts everything after it up by one.
  const deck5 = ["a", "b", "c", "d", "e"].map(id => ({ id, when: null }));
  const ids = (d) => d.map(r => r.id).join("");
  const moves = [
    ["a", 3, "bcade", "down: lands at 3, not 4"],
    ["e", 1, "eabcd", "up to the top"],
    ["b", 5, "acdeb", "down to the end"],
    ["c", 3, "abcde", "to where it already is: unchanged"],
    ["a", 99, "bcdea", "past the end is clamped"],
    ["a", 0, "abcde", "below 1 is clamped"],
    ["zz", 2, "abcde", "an id not in the deck changes nothing"],
  ];
  if (typeof globalThis.__moveRow !== "function") {
    problems.push("moveRow never got defined");
  } else {
    for (const [id, pos, want, why] of moves) {
      const got = ids(globalThis.__moveRow(deck5, id, pos));
      if (got !== want)
        problems.push(`moveRow: ${why} - got ${got}, want ${want}`);
    }
    // and it must not mutate the deck it was handed
    globalThis.__moveRow(deck5, "a", 4);
    if (ids(deck5) !== "abcde")
      problems.push("moveRow mutated the deck it was given");
    console.log("moveRow: " + moves.length + " case(s) checked");
  }

  // The affordance has to be on the row, or the feature is undiscoverable.
  const deckHTML = (boxes["deck"] || {}).innerHTML || "";
  if (!/data-move="/.test(deckHTML))
    problems.push("deck rows offer no way to move a slide by position");

  // Two numbers per row: its position in this list, and the slide's own number
  // in library.pptx. The second is what an author reads against Templafy while
  // they set the order, and it has to be there AND be told apart from the
  // first - a bare second integer in the gutter would be worse than none.
  const srcs = [...deckHTML.matchAll(/class="src"[^>]*>s(\d+)</g)].map(m => m[1]);
  if (!srcs.length)
    problems.push("deck rows do not show the slide's number in library.pptx");
  if (/>s\?</.test(deckHTML))
    problems.push("a deck row could not find its slide number");
  if (!/title="Slide \d+ in library\.pptx"/.test(deckHTML))
    problems.push("the slide number on a row does not say what it is");
  const poolHTML = (boxes["pool"] || {}).innerHTML || "";
  if (poolHTML.includes("pool-item") && !/class="src"/.test(poolHTML))
    problems.push("a slide out of the deck does not show its number either");
  console.log("slide numbers: " + srcs.join(", ") + " on the deck rows");

  // "Slide 47 in Templafy - where is it here?" has to be answerable. The
  // finder reports the row, and reports it differently when the slide is not
  // in the deck, because that is the answer the author most needs to act on.
  // Through getElementById, not `boxes`: the stub only makes an element once
  // the script has asked for one, and the note is not written to until the
  // finder actually runs.
  const note = document.getElementById("goto-note");
  const box = document.getElementById("goto-slide");
  const go = document.getElementById("goto-go");
  if (!note || !box || !go || typeof go.onclick !== "function") {
    problems.push("there is no way to go to a slide by its number");
  } else {
    const say = () => (note.innerHTML || note.textContent || "");
    box.value = "3";              // "fees" - in the deck
    go.onclick();
    if (!/s3/.test(say()) || /not in the deck/.test(say()))
      problems.push("goto: a slide in the deck was not reported as such - " + say());
    box.value = "4";              // "dropped" - in the library, not the deck
    go.onclick();
    if (!/not in the deck/.test(say()))
      problems.push("goto: a slide out of the deck was not flagged - " + say());
    box.value = "999";
    go.onclick();
    if (!/slides 1 to 4/.test(say()))
      problems.push("goto: a number with no slide behind it said " + say());
    console.log("go to slide: in the deck, out of the deck, and out of range");
  }

  // The viewer shows one of two things and must not confuse them: a PNG that
  // PowerPoint exported, or our own SVG approximation. Showing the second while
  // implying the first is how a preview stops being evidence.
  const V = globalThis.__viewer;
  if (!V) {
    problems.push("the deck viewer never got defined");
  } else {
    await V.load("0123456789abcdef");
    const stage = (boxes["viewer-stage"] || {}).innerHTML || "";
    const strip = (boxes["viewer-strip"] || {}).innerHTML || "";
    const how = (boxes["viewer-how"] || {}).innerHTML || "";
    if (!/<img class="slide" src="\/api\/builds\/x\/png\/1"/.test(stage))
      problems.push("viewer stage does not show the exported PNG");
    if ((strip.match(/<img class="fthumb"/g) || []).length !== 2)
      problems.push("filmstrip does not show one exported PNG per slide");
    if (!/loading="lazy"/.test(strip))
      problems.push("filmstrip images are not lazy - 60 slides would load at once");
    // The preview only earns the right-hand column once a deck exists; before
    // that the two panels want the width.
    if (!(boxes["build-cols"] || {}).classList.contains("with-deck"))
      problems.push("the build screen did not give the preview its column");
    if (!/rendered from your library/.test(how))
      problems.push("the viewer does not name the renderer that drew it");
    // The library preview cannot show filled values. If that stops being said
    // on screen, someone will read {{ClientName}} as a bug in their deck.
    const dis = (boxes["viewer-note"] || {});
    if (dis.hidden !== false || !/Placeholders show unfilled/.test(dis.innerHTML || ""))
      problems.push("the library preview does not disclaim unfilled placeholders");
    const approx = V.big({ index: 1, png: null, svg: DEMO_SVG });
    if (!/<svg class="slide"/.test(approx))
      problems.push("viewer cannot fall back to the SVG renderer");
    const gone = V.big({ index: 1, png: null, svg: null, error: "boom" });
    if (!/did not render/.test(gone) || !/boom/.test(gone))
      problems.push("a slide that failed to render does not say so");
    console.log("viewer: PNG, SVG fallback and failure all render");
  }

  // The Values screen previews a date in the chosen format. If that preview
  // disagrees with engine/bindings.date_formats() it is worse than no preview,
  // because it is believed. These are the engine's exact strings.
  const FORMATS = {
    long_comma: "November 30, 2026", day_month: "30 November 2026",
    abbr_comma: "Nov 30, 2026", day_abbr: "30 Nov 2026",
    us_slash: "11/30/2026", eu_slash: "30/11/2026",
    iso: "2026-11-30", raw: "20261130",
  };
  if (typeof globalThis.__formatted !== "function")
    problems.push("formatted() never got defined");
  else for (const [k, v] of Object.entries(FORMATS)) {
    const got = globalThis.__formatted("20261130", k);
    if (got !== v) problems.push(`date format ${k}: page says "${got}", engine says "${v}"`);
  }
  console.log("date formats: " + Object.keys(FORMATS).length + " checked against the engine");

  // The suggestion report quotes field names and values that came out of the
  // author's own files. Those are text, not markup, and must be escaped before
  // any formatting is applied.
  const MD = [
    ["# Title", "<h1>Title</h1>", "heading"],
    ["- one", "<ul><li>one</li></ul>", "list"],
    ["`AuditType`", "<p><code>AuditType</code></p>", "inline code"],
    ["**bold**", "<p><strong>bold</strong></p>", "bold"],
    ["> careful", "<blockquote>careful</blockquote>", "blockquote"],
    ["- `<script>alert(1)</script>`",
     "<ul><li><code>&lt;script&gt;alert(1)&lt;/script&gt;</code></li></ul>",
     "markup in a value is escaped, not run"],
  ];
  if (typeof globalThis.__md !== "function") problems.push("md() never got defined");
  else for (const [src, want, why] of MD) {
    const got = globalThis.__md(src);
    if (got !== want) problems.push(`markdown ${why}: got ${got}, want ${want}`);
  }
  console.log("markdown: " + MD.length + " case(s) checked");

  // ---------------------------------------------------------- mark text
  //
  // The overlay is the whole feature: a box in the wrong place means an author
  // marks the wrong words and never finds out. This stub has no layout, so the
  // check reads what the screen wrote - that boxes are drawn, that each is
  // placed as a share of the slide rather than in pixels (the picture and the
  // overlay have to scale together), and that the picture is the real page.
  try {
    __showTab("mark");
    await __renderMark();
    const stage = (boxes["mk-stage"] || {}).innerHTML || "";
    const drawn = (stage.match(/class="mk-box/g) || []).length;
    if (drawn !== 2) {
      problems.push("the mark screen drew " + drawn + " box(es), expected 2");
    }
    // 914400 of 12192000 EMU is 7.5% from the left, which is exactly where the
    // shape sits on the slide. A pixel value here would be a bug.
    if (!/left:7\.5000%/.test(stage)) {
      problems.push("a mark box is not placed as a share of the slide");
    }
    if (!/\/slides\/1\/png/.test(stage)) {
      problems.push("the mark screen is not showing the real rendered page");
    }
    const words = (__mkShapeHTML(__MK.map.shapes[0], 0)
      .match(/class="mk-w/g) || []).length;
    if (words !== 3) {
      problems.push("opening a shape gave " + words
        + " clickable word(s), expected 3");
    }
    const rail = (boxes["mk-here"] || {}).innerHTML || "";
    if (!rail.trim()) problems.push("the mark screen rail is empty");
    console.log("mark text: " + drawn + " exact box(es), " + words
      + " words selectable in the opened shape");
  } catch (err) {
    problems.push("the mark screen threw: " + err.message);
  }

  // ------------------------------------------------- the questions form
  //
  // The chicken and egg this form exists to break: the field catalogue is
  // built from answer sets, so a library that has just been marked up has an
  // empty one - and the author is asked to hand-write the first JSON file
  // precisely when they know least. The placeholders already say which fields
  // will be asked for, so the form can be built from those instead.
  //
  // The canned library has `{{FeeTotal}}` with no binding and no answer set
  // has ever mentioned it. It has to appear as a question anyway.
  try {
    __showTab("questions");
    __renderQuestionForm();
    const form = (boxes["qf-form"] || {}).innerHTML || "";
    const asked = (form.match(/class="q[ "]/g) || []).length;
    if (asked < 5) {
      problems.push("the questions form drew " + asked
        + " question(s), expected 5 (4 from answer sets, 1 from a placeholder)");
    }
    if (!/FeeTotal/.test(form)) {
      problems.push("the questions form does not ask for a field that only a "
        + "placeholder mentions");
    }
    // The build form and this one must render a field the same way: the same
    // date field has to be a date control on both.
    if (!/data-newans="DueDate"[^>]*type="date"|type="date"[^>]*data-newans="DueDate"/
      .test(form)) {
      problems.push("a date field is not a date control on the questions form");
    }
    const note = (boxes["qf-note"] || {}).innerHTML || "";
    if (!/never\s+been\s+answered/.test(note)) {
      problems.push("the questions form does not say which fields are new");
    }
    // The derived list is a starting point. An author who does not want to be
    // asked something takes it off; one who needs a question nobody derived
    // puts it on. Neither should be able to produce two questions for one
    // thing - `Client` beside `client` is two fields and neither fills.
    if (!/data-qdrop="FeeTotal"/.test(form)) {
      problems.push("a question on the form cannot be removed");
    }

    __QFOFF.add("FeeTotal");
    __renderQuestionForm();
    const fewer = ((boxes["qf-form"] || {}).innerHTML || "")
      .match(/class="q[ "]/g) || [];
    if (fewer.length !== asked - 1) {
      problems.push("removing a question left " + fewer.length
        + " on the form, expected " + (asked - 1));
    }
    // FeeTotal is a placeholder with no binding, so the deck needs it. Saying
    // nothing here would let an author quietly remove the question that fills
    // a slide.
    const warned = (boxes["qf-note"] || {}).innerHTML || "";
    if (!/used\s+on a slide/.test(warned)) {
      problems.push("removing a question the deck needs is not flagged");
    }

    const box = document.getElementById("qf-new");
    box.value = "Engagement partner";
    __qfAdd();
    const added = (boxes["qf-form"] || {}).innerHTML || "";
    if (!/Engagement_partner/.test(added)) {
      problems.push("a question added by hand did not appear on the form");
    }

    box.value = "engagement  PARTNER";
    __qfAdd();
    const twice = ((boxes["qf-form"] || {}).innerHTML || "")
      .match(/data-newans="Engagement_partner"/g) || [];
    if (twice.length !== 1) {
      problems.push("the same question was added twice under a different case");
    }

    __QFOFF.clear();
    __QFADD.clear();
    __renderQuestionForm();
    console.log("questions form: " + asked
      + " question(s), including one no answer set has ever carried; "
      + "removable and extendable");
  } catch (err) {
    problems.push("the questions form threw: " + err.message);
  }

  console.log("\n" + calls.length + " API call(s): "
    + [...new Set(calls.map(u => u.split("?")[0]))].join(", "));

  if (problems.length) {
    console.log("");
    problems.forEach(p => console.log("  ! " + p));
    console.log("\n" + problems.length + " problem(s)");
    process.exit(1);
  }
  console.log("\nthe page initialises and every region has content");
})();
