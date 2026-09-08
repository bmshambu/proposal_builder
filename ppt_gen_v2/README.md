# ppt_gen_v2 — our own proposal-deck engine

**Status:** Milestone 1 done — the engine builds and validates decks from an owned
library, and any `.pptx` can be added as a template without touching code. No editor
UI yet.

## The pivot (why v2 exists)

v1 (`../`) proved we can rebuild Templafy's decks **deterministically** — ~92% of
every deck reproduced exactly, 100% correct slide selection. It works. But it works
by **chasing Templafy**: it generates reference decks *with* Templafy, then reverse-
engineers the rules by comparing them ("harvesting"). That means:

- We still depend on Templafy to *learn* the rules.
- Every time a template changes, we must re-generate and re-harvest.
- We infer rules by observation — powerful, but always a step behind the source.

**Our lead's direction:** stop chasing. Don't recreate Templafy (their template
editor is hard to update anyway). Instead **build our own simple thing**, borrowing
the good ideas from Templafy:

1. A **simple template creator/editor** — we author and own the branded blocks.
2. A **PPT builder** — assembles a finished deck from *our* blocks and *our* rules.

The rules stop being *harvested* (black-box, inferred) and become *authored*
(white-box, explicit). We own the whole library, so updating a template is just
editing a slide — no Templafy in the loop at all.

## Quick start

```bash
cd ppt_gen_v2

python build.py list                     # registered templates
python build.py inspect demo             # blocks, where their ids come from, rules check
python build.py preview demo             # render every block to an SVG contact sheet

# build two decks from ONE library and two answer sets
python build.py make demo --answers templates/demo/answers.baseline.json  --out out/northwind.pptx -v
python build.py make demo --answers templates/demo/answers.expansion.json --out out/globex.pptx    -v

python tests/test_engine.py              # 70 tests, standard library only
```

Every build runs `../validate_pptx.py` before reporting success — decision D5: a deck
that doesn't validate never reaches a user.

## Adding your own template — no code changes

Point `import` at any `.pptx`. The deck is copied **byte for byte** and never
modified:

```bash
python build.py import "Firm Proposal Template.pptx" --name firm
python build.py inspect firm
```

`import` reads the deck, derives a block id per slide from its **title**, writes
those ids to a `blocks.json` sidecar, and generates a starter `rules.json` whose
baseline is every slide in the deck's own order. **It builds immediately** — as a
faithful copy of the source deck — and you narrow it from there:

```bash
python build.py rename firm scope_of_work_expanded scope_expansion
# then edit templates/firm/rules.json: move optional blocks out of `baseline`,
# give them a `when` and an `insert_after`
```

Nothing about this touches the engine. A template is just a folder:

```
templates/<name>/
  template.json       name, description, which file is the library
  library.pptx        the deck someone authored in PowerPoint
  blocks.json         slide -> block id (so an unedited deck can be named)
  rules.json          which blocks a given answer set produces
  data_sources.json   stubs for external systems (optional)
```

### Where a block id comes from

Resolved per slide, first hit wins:

| | Source | Survives a reorder? | Needs the deck edited? |
|---|---|---|---|
| 1 | `{{block:cover}}` marker on the slide | yes | yes |
| 2 | `blocks.json` sidecar | yes | **no** |
| 3 | slide title, slugified | no | no |
| 4 | position (`slide7`) | no | no |

The sidecar is what makes an existing firm template usable untouched. `inspect`
marks anything resting on 3 or 4 with `?`, because reordering the deck renames it.
It also warns when a slide has been retitled since import, since that usually means
the id no longer fits.

The two demo builds differ exactly as the rules say they should:

| | baseline answers | `AuditType = Expansion of Services` |
|---|---|---|
| slides | 7 | 8 |
| `expansion_detail` | absent | inserted after `approach` |
| `scope` | `scope` | variant `scope_expansion` |
| `{{ClientName}}` | Northwind Manufacturing plc | Globex International Holdings |

## Slide previews, without LibreOffice

`engine/svg.py` draws a slide straight from its OOXML — shapes, theme colours,
text, tables and images — as SVG, in-process:

```bash
python build.py preview demo --out out/sheet.html
```

Nine slides render in **~60 ms** for **~31 KB** total. Nothing to install on the
server, so importing a 60-slide template stays a normal request rather than a
background job, and previews scale crisply instead of being fixed-resolution PNGs.

Placeholder runs are tagged `class="ph"` in the SVG itself, so the author console
can light up exactly where values land — including placeholders PowerPoint split
across runs, which are stitched with the same pass the build path uses.

**It is an approximation, not a brand proof.** Text wrapping is estimated (real
metrics need the real font), gradients render as their first stop, and charts,
SmartArt and embedded objects are drawn as a labelled box rather than guessed at.
`engine/svg.py` also exposes `block_card()` — a text-only description of a block —
as the zero-risk fallback.

## How it works

```
templates/<name>/
  library.pptx     one deck someone authored in PowerPoint
  blocks.json      slide -> block id (or a {{block:<id>}} marker on the slide itself)
  rules.json       baseline spine + conditional blocks + variants + placeholder bindings
  data_sources.json  stubs for the external systems (Milestone 4)
        │
        ▼
  answers.json  ──►  rules engine  ──►  ordered block list
                     assembler     ──►  one fresh slide part per block, from the library
                     placeholders  ──►  {{Name}} filled inside <a:t> only
                     validator     ──►  structural + well-formedness check
                                        │
                                        ▼
                                  out/<deck>.pptx
```

Assembly is *structurally simpler* than v1's, on purpose: every block comes from one
library deck, so the masters, layouts, theme and media the output needs are already in
the package. There is no dependency closure to chase, no foreign master to graft, no
media to rename — the whole class of v1's hardest bugs is gone by construction.

## Folder map

```
ppt_gen_v2/
  README.md                  <- you are here
  build.py                   <- CLI: list / import / inspect / rename / make
  engine/
    ooxml.py                 <- rel ids, content types, slide surgery (the v1 rules)
    placeholders.py          <- run stitching + table-safe {{Name}} fill
    library.py               <- read library.pptx, resolve block identity
    template.py              <- a template is a folder; import any .pptx into one
    svg.py                   <- draw a slide from its OOXML (previews, no deps)
    rules.py                 <- conditions, spine, insert_after, variants
    bindings.py              <- placeholder -> field / data source / literal
    assemble.py              <- blocks -> valid .pptx
  templates/<name>/          <- one folder per template; demo/ is the worked example
  tools/                     <- demo-library generator + contact-sheet builder
  tests/test_engine.py       <- regression suite (70 tests)
  docs/
    v1-learnings.md          <- the OOXML knowledge v1 paid for. REFERENCE.
    v2-plan.md               <- vision, architecture, roadmap, decisions
    authoring-a-library.md   <- how a designer authors a library deck
```

## Authoring a template

See `docs/authoring-a-library.md`. Two routes, both supported:

- **Import an existing deck** — `build.py import`, then name the blocks with
  `rename`. The deck is never modified. This is the route for a firm template you
  already have.
- **Author for the engine** — a designer makes a normal branded deck, puts an
  off-canvas text box reading `{{block:cover}}` on each slide, and types
  `{{ClientName}}` wherever a value belongs. Best when you own the deck, since the
  ids then travel with the slides.

Either way you still have to type `{{Placeholders}}` into the slides in PowerPoint —
that is the one thing import cannot guess, and deliberately so: inferring which text
is dynamic is exactly the v1 problem v2 exists to delete.

## What's next

`docs/v2-plan.md` → Roadmap. Milestone 2 (golden-file regression against approved
decks) and Milestone 3 (the editor UI) are the next moves; Milestone 4 wires the real
data sources, which is the last ~8% v1 identified.

## Ground rules (unchanged from v1)

- Templates and client decks are **confidential** — everything runs locally, nothing
  leaves the machine. The committed demo library is synthetic; no real template is in
  this repo.
- Deterministic, no AI in the deck-building path. Reproducible and auditable — the
  test suite asserts byte-identical output for identical inputs.
- Standard library only in `engine/`. No dependencies to keep current.
