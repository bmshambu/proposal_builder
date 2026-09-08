# v2 plan — our own template engine

> Read `README.md` first for the *why*. This doc is the *what* and *how*.

---

## 1. Vision

Build a small, owned system that does what Templafy does for us — assemble a branded
50–60 slide proposal draft from answers — **without Templafy, and simple enough that our
own team maintains it.**

Two components, mirroring the lead's framing:

1. **Template creator/editor** — where we author and own the branded content and the
   rules that assemble it.
2. **PPT builder** — the deterministic engine that turns *(answers + template)* → a valid,
   branded `.pptx`.

The guiding principle: **separate the three things Templafy tangles together.**

| Concern | What it is | Owned/authored by | Stored as |
|---|---|---|---|
| **Content blocks** | branded slides / sections | designers, in PowerPoint | slides in a master library deck |
| **Selection rules** | which blocks a given answer set produces | us, in the editor | a plain rules file (JSON/YAML) |
| **Field bindings** | how placeholders get filled | us, in the editor | placeholder map + data-source bindings |

Keeping these separate is the whole design. A brand refresh touches only *content*. A new
optional section touches only *rules*. A new data source touches only *bindings*.

## 2. Key design decisions (proposed — confirm next session)

**D1. Author blocks in PowerPoint, don't generate slides from code.**
Branding fidelity is brutal to reproduce from scratch (themes, masters, exact geometry).
Designers already know PowerPoint. So a "block" is a *real branded slide someone made*,
and the engine only ever **selects, orders, and fills placeholders** — never draws.
→ This is the single biggest de-risking choice. It also means the engine is the same
   "copy slides out of a source deck + keep it valid" engine we already built in v1.

**D2. One master library deck + one rules file (for MVP).**
Simplest source of truth: a single `library.pptx` where each slide (or contiguous
section) is a named block, plus `rules.json` mapping answers → blocks and defining
placeholders. Reuses v1's assembler most directly (source deck = our library).
→ Alternative (later): per-block `.pptx` fragments + manifest. More modular, more
   assembly complexity. Defer unless one-master editing gets unwieldy.

**D3. Explicit placeholders, not inferred tokens.**
Designers type `{{ClientName}}`, `{{DueDate}}`, `{{City}}`, `{{LeadPartner}}` directly in
the slide. The engine replaces them (table-safe `<a:t>` method from v1). This deletes the
entire "is this text dynamic or static?" problem that bit us in v1.

**D4. Reuse v1's raw-OOXML assembler; do NOT switch to python-pptx blindly.**
`python-pptx` is friendlier but is lossy/limited on complex branded slides and doesn't
give us the closure/rel control we need. We already have a proven emitter. Keep it.
→ `python-pptx` may still be handy for *authoring-time tooling* (e.g. reading a library
   deck to list slides), just not for the fidelity-critical assembly path.

**D5. Validate every build.** `validate_pptx.py` runs on every output. A build that
doesn't pass never reaches a user. Non-negotiable — it's how we keep the "no repair
dialog" guarantee.

## 3. Architecture

```
                    ┌──────────────────────────────────────────────┐
   AUTHOR TIME      │  Template creator/editor  (web UI, our team)  │
                    │  • register blocks from library.pptx          │
                    │  • set each block's trigger rule              │
                    │  • declare placeholders + data bindings       │
                    └───────────────┬──────────────────────────────┘
                                    │ writes
                                    ▼
         library.pptx  +  rules.json / bindings.json        (the OWNED template)
                                    │
                    ┌───────────────┴──────────────────────────────┐
   BUILD TIME       │  PPT builder  (deterministic engine)          │
   answers ───────► │  1. rules engine → ordered list of blocks     │
                    │  2. assembler → copy blocks + closure → PPTX  │  ◄── reuse v1
                    │  3. placeholder fill (tokens + data)          │  ◄── reuse v1
                    │  4. validate                                  │  ◄── reuse v1
                    └───────────────┬──────────────────────────────┘
                                    ▼
                            branded_deck.pptx
```

### Data model (first cut — refine when we build)

```jsonc
// rules.json
{
  "baseline": ["cover", "about_us", "our_team", "approach", "fees", "contacts"],
  "blocks": {
    "cover":            { "slides": ["Cover"],            "when": "always" },
    "expansion_detail": { "slides": ["ExpansionDetail"],  "when": { "field": "AuditType", "eq": "Expansion of Services" },
                          "insert_after": "approach" },
    "scope":            { "slides": ["Scope"],            "when": "always",
                          "variant": { "when": { "field": "AuditType", "eq": "Expansion of Services" }, "slides": ["ScopeExpansion"] } }
  },
  "placeholders": {
    "{{ClientName}}": { "from": "field", "field": "FullClientName" },
    "{{DueDate}}":    { "from": "field", "field": "DueDate", "format": "long_comma" },
    "{{City}}":       { "from": "field", "field": "City" },
    "{{LeadPartner}}":{ "from": "data",  "source": "profile", "key": "lead_partner" }  // data-driven, wired later
  }
}
```

- **`when`** — the trigger condition. Start with simple `always` / `{field, eq, value}` /
  `{field, in, [...]}`; add `and`/`or` only when a real template needs it.
- **`insert_after` / ordering** — the baseline array is the spine; conditional blocks
  declare where they slot in. (v1 learned anchors matter; here we declare them.)
- **`variant`** — a block whose *content* changes with an answer (v1's "swap"): pick a
  different named slide from the library.
- **`placeholders.from`** — `field` (from the answers) or `data` (external source; the
  binding *names the source* so the last 8% is wiring, not research — same conclusion as v1).

### How a block maps to library slides — **settled**

Each block names one or more slides in `library.pptx`. All three options we listed
turned out to be needed, as a fallback chain rather than a choice — because option (b)
alone would mean editing every slide of a deck we did not author, which is exactly the
friction that makes adding a template feel like a code change:

| | Mechanism | Survives a reorder? | Needs the deck edited? |
|---|---|---|---|
| 1 | `{{block:cover}}` marker box on the slide | yes | yes |
| 2 | `blocks.json` sidecar, keyed by slide part | yes | **no** |
| 3 | slide title, slugified | no | no |
| 4 | position (`slide7`) | no | no |

First hit wins. **(2) is what makes an existing firm template usable untouched**, and
it is what `import` writes, seeded from (3). `inspect` flags any block resting on
(3) or (4), and reports drift when a slide is retitled after import.

### A template is a folder

```
templates/<name>/
  template.json       name, description, which file is the library
  library.pptx        the deck someone authored in PowerPoint
  blocks.json         slide -> block id
  rules.json          which blocks a given answer set produces
  data_sources.json   stubs for external systems (optional)
```

The engine only ever takes paths, so adding a template is a file operation. `build.py
import <deck.pptx>` creates the folder from any deck; `list` / `inspect` / `rename` /
`make` operate on it. This is the CLI half of the template editor in §4 — the UI will
drive the same functions rather than reimplementing them.

## 4. The template editor (author time)

MVP can be **thin** — even hand-edited `rules.json` works to prove the engine. Build the
UI once the engine is solid. Target editor capabilities, simplest first:

1. **List blocks** — read `library.pptx`, show each slide as a thumbnail/title with its
   detected block marker.
2. **Set a rule** — for each block, a friendly condition builder ("show when AuditType is
   Expansion of Services"). Writes `rules.json`.
3. **Declare placeholders** — scan slides for `{{…}}`, let the user bind each to a field or
   data source. Writes `bindings.json`.
4. **Preview / validate** — build a sample deck from sample answers, run the validator,
   show green/red.

Reuse the v1 FastAPI + templates shell for this.

## 5. Reuse map (what we lift from v1)

See `v1-learnings.md` §4 for detail. In short:
- **Lift wholesale:** `assemble()` + helpers (the valid-PPTX emitter), `_apply_tokens()`
  (table-safe), `validate_pptx.py`, `pptx_forensics.py`, `diff_decks.py`, the FastAPI shell.
- **Leave behind:** `align()`, `varied_fields()`, `make_payloads.py`, `map_decks.py`,
  anything that runs Templafy to learn rules.

## 6. Roadmap

### Milestone 1 — prove owned-library assembly (engine only, no UI) — **DONE**
- [x] A 9-block `library.pptx`, with `{{…}}` placeholders, a real `<a:tbl>` slide to
      guard the `<a:t>` fix, and a placeholder split across four runs to guard the
      run-stitching pass. Generated by `tools/make_demo_library.py` rather than
      hand-authored, so it is synthetic, committable and reproducible — a real
      template is still authored in PowerPoint (D1 unchanged).
- [x] `templates/demo/rules.json` — baseline spine + one conditional block
      (`expansion_detail`, `insert_after: approach`) + one variant (`scope` →
      `scope_expansion`).
- [x] Assembler in `engine/`, decoupled from harvesting: *(library.pptx, rules.json,
      answers)* → valid `.pptx`. It came out **smaller** than v1's — one library deck
      means the masters/layouts/theme/media are already in the package, so the
      dependency-closure, master-grafting and media-remap machinery is not needed at
      all. What survives is the id/content-type/rels discipline (`engine/ooxml.py`).
- [x] Placeholder fill via the fixed `<a:t>` pattern, **plus** a run-stitching pre-pass
      so a placeholder PowerPoint fragmented across runs is still substituted
      (risk §8, now closed and tested).
- [x] `validate_pptx.py` runs on every build (D5); v1's copy is imported, not forked.
- [x] 55-test regression suite (`tests/test_engine.py`), standard library only.
- [x] **Template onboarding** (added after the milestone, on the "make it configurable"
      ask): a template is a folder, and `build.py import` turns any `.pptx` into one —
      copied byte for byte, named from slide titles, with a starter `rules.json` that
      builds immediately. `list` / `inspect` / `rename` / `make` round it out. Adding a
      template never touches code.

**Exit test — passed:** `answers.baseline.json` → 7 slides, `answers.expansion.json` →
8 slides with the conditional block at its anchor and the scope variant swapped, both
validating clean, both byte-identical on rebuild.
*Still owed: open one in desktop PowerPoint to confirm no repair dialog — the
validator is a strong proxy, not the real thing.*

### Milestone 2 — the rules engine, properly
- [ ] Condition types (`always`, `eq`, `in`, ordering via `insert_after`, `variant`).
- [ ] Golden-file regression: keep approved output decks, `diff_decks` every build.
- [ ] Date formatting + placeholder-from-field/data plumbing (data stubbed).

### Milestone 3 — the template editor (UI)
- [ ] Block list from `library.pptx` (block-marker detection — decide D-marker approach).
- [ ] Rule builder → `rules.json`; placeholder binder → `bindings.json`.
- [ ] In-UI preview + validate.

### Milestone 4 — data-driven slides
- [ ] Wire the named sources (profile → partner, finance → fees, RFP → table).
- [ ] Same conclusion as v1: once sources are connected, the last ~8% closes.

### Milestone 5 — scale to multiple templates
- [ ] Each template = its own `library.pptx` + `rules.json` in its own folder.
- [ ] Templates never interfere; one app serves the whole catalogue.

## 7. Open decisions — status after Milestone 1

1. **Block-marker mechanism** — **settled: a four-step identity chain**, because
   requiring a marker would mean editing every slide of a template we did not author:
   (1) an off-canvas `{{block:<id>}}` marker box, (2) a `blocks.json` sidecar keyed
   by slide part, (3) the slide title slugified, (4) position. Marker and sidecar
   both survive reordering; `inspect` flags anything resting on 3 or 4, and reports
   drift when a slide is retitled after import. A duplicate *declared* id (marker or
   sidecar) is a hard error; a duplicate *guessed* id is disambiguated, since two
   slides may legitimately share a heading. (`docs/authoring-a-library.md` §§0, 2.)
2. **One master deck vs per-block fragments** — **single deck confirmed for now.**
   It is what makes the assembler small (no closure to chase) and prevents branding
   drift by construction. Revisit only if one-deck editing gets unwieldy.
3. **Rules format** — **settled: JSON.** No dependency, and the editor will write it
   rather than humans, so YAML's hand-editing edge does not pay for itself. Comments
   go in `_comment` keys.
4. **Ordering model** — **linear spine + `insert_after` is enough so far.** It carried
   the demo template without strain. Sections stay unbuilt until a real template needs
   them; note the current model appends a block whose anchor is absent, which is
   forgiving but would hide a typo in a large template.
5. **How designers hand off a library** — **settled: they hand over the deck, unchanged.**
   `build.py import <deck.pptx>` copies it byte for byte into a template folder,
   derives a block id per slide from its title, writes a `blocks.json` sidecar and a
   starter `rules.json` (baseline = every slide, in the deck's own order). The result
   builds immediately and is narrowed from there. This is what makes "port it to our
   office template" a command rather than a code change — the engine only ever takes
   paths, and a template is just a folder.
6. **Placeholder syntax** — **settled: `{{Field}}`, and the split-run risk is closed.**
   `engine/placeholders.py` stitches a placeholder fragmented across `<a:r>` runs back
   together before substituting, editing only `<a:t>` bodies so no run is added,
   removed or reordered. The demo library carries a four-run split placeholder so the
   guard stays tested.

11. **Slide previews are rendered in-process to SVG, not by LibreOffice.**
    LibreOffice is free for commercial use and was the obvious choice, but it
    costs a ~400 MB server install, 1–2 s per slide (making import a background
    job), and produces flat fixed-resolution PNGs. `engine/svg.py` draws the
    slide from its own OOXML instead: ~60 ms for nine slides, ~31 KB, no
    dependency, and — the deciding factor — placeholder runs stay *tagged* in
    the output, so the console can highlight where values land instead of
    showing a picture of it. The trade is fidelity: wrapping is estimated and
    unrenderable content (charts, SmartArt, OLE) is drawn as a labelled box.
    `render_template()` is the seam if a high-fidelity mode is ever wanted.

### New decisions taken while building

7. **Unfilled placeholders stay visible.** A missing value leaves `{{Foo}}` on the
   slide and is reported, rather than blanking silently — an author sees the gap.
   `--strict` turns that into a build failure for CI.
8. **Speaker notes are dropped.** A notes part back-references its slide, and a block
   used twice would leave two slides pointing at one notes part. The build reports the
   count. Revisit if authors actually put notes in libraries.
9. **Conditions compare as strings, case- and whitespace-insensitively.** Answers come
   from forms; a rule silently missing because of a trailing space is a bad failure
   mode.
10. **The demo library is generated, not hand-made** (`tools/make_demo_library.py`), so
    the repo carries a reproducible, non-confidential fixture and the tests need no
    real template.

## 8. Risks / watch-items

- ~~**PowerPoint splits `{{ClientName}}` across text runs**~~ — **closed in Milestone 1.**
  `engine/placeholders.py` stitches split placeholders before substituting; the demo
  library keeps a four-run split placeholder so the guard cannot rot.
- ~~**Branding drift**~~ — **closed by construction**: one `library.pptx` means one set
  of masters. It re-opens the moment we adopt per-block fragments (decision 2).
- **Untested against a real template.** Everything so far is proven against a synthetic
  9-slide library. A designer-authored deck brings GUID-style rel ids, several masters,
  media, embedded objects and notes. The engine keeps v1's id/content-type discipline
  for exactly this, but it has not met one yet — do that before trusting it.
  **`build.py import <your-deck.pptx>` is now the way to try it**, and it is the next
  thing worth doing: the import path is exercised against an unmarked fixture, not a
  real firm deck.
- **Title-derived ids are a starting point, not an answer.** Import names blocks from
  slide titles, which is good enough to get a template building the same day but
  produces ids like `who_you_will_work_with`. Expect to spend a pass renaming them
  (`build.py rename`) before the rules read well.
- **No golden-file regression yet.** `diff_decks.py` is ready to reuse (Milestone 2);
  until then the suite proves validity and determinism, not visual fidelity.
- **Scope creep into "rebuild Templafy."** Resist. The editor stays simple: select, order,
  fill. No live data editing, no WYSIWYG slide design — that's PowerPoint's job.
