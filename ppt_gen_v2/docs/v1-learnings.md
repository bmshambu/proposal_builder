# What v1 taught us — technical reference for v2

This is the knowledge we paid for while building the harvesting engine. **Most of it is
about one thing: emitting a `.pptx` that PowerPoint opens without a "repair" dialog.**
That is the hard, non-obvious part of generating decks, and v2 inherits every bit of it.
Keep this open while building the v2 assembler.

---

## 1. A `.pptx` is a ZIP of XML parts

A PowerPoint file is a ZIP (the OPC / OOXML format). Inside:

```
[Content_Types].xml          declares the content type of every part
_rels/.rels                  package root relationships -> ppt/presentation.xml
ppt/presentation.xml         the deck: ordered list of slide ids + master ids
ppt/_rels/presentation.xml.rels   maps those ids -> actual slide/master parts
ppt/slides/slideN.xml        one file per slide (the shapes + text)
ppt/slides/_rels/slideN.xml.rels  that slide's links -> layout, media, embeds, hyperlinks
ppt/slideLayouts/…           layouts (each slide points at exactly one)
ppt/slideMasters/…           masters (each layout points at one; carries theme)
ppt/theme/…                  colors/fonts (branding lives here + in masters/layouts)
ppt/media/…                  images, and the raw bytes of embedded objects
ppt/embeddings/…             OLE objects (embedded Excel, etc.)
```

**Everything is connected by relationships (rels) and content types.** Break one link
and PowerPoint offers to "repair" — which is the failure mode we spent most of v1 fighting.

## 2. The rules PowerPoint is strict about (each one = a repair dialog)

These are exactly the checks our validator (`../validate_pptx.py`) ended up making.
**Re-implement all of them in v2; the validator itself is directly reusable.**

| Rule | What breaks it | Symptom |
|---|---|---|
| **Unique relationship IDs** | Two `Id="rIdN"` in the same `.rels` | `REL_ID_INVALID` |
| **No dangling targets** | A rel points at a part not in the ZIP | repair / `REL_TARGET_MISSING` |
| **Every referenced rId exists** | Slide XML uses `r:id="X"` with no matching rel | `missing-rel` |
| **Content-type Overrides** | master/layout/theme/slide need a *specific* `<Override>`, not the generic `.xml` Default | silent repair |
| **Well-formed XML** | Any malformed slide part | `XML_MALFORMED` (unreadable content) |
| **Unique part names** | Same path written twice | `duplicate-part` |
| **Unique sldId / sldMasterId** | Duplicate `id=` in presentation.xml | `REL_ID_INVALID` |

### Gotchas that cost us real time (do NOT relearn these)

- **Templafy uses GUID-style rel IDs** (`R04cd45cb…`), not `rId7`. So "find the max
  `rIdN` and add one" computed a *wrong* max and collided. **Fix:** generate new IDs by
  skipping every ID already present, whatever its shape. (`_new_rid()` in `harvest.py`.)
  v2 authors its *own* IDs so this is easier — but the "must be globally unique in the
  file" rule still holds.

- **Text replacement must not touch markup.** Our first token-replacer rewrote whole
  paragraphs and corrupted slides. The safe approach: replace only *inside* each
  `<a:t>…</a:t>` text run, unescape → replace → re-escape.

- **The `<a:t>` table-corruption bug (important).** The regex `<a:t[^>]*>` also matches
  `<a:tbl>`, `<a:tc>`, `<a:tr>`, `<a:tblPr>`, `<a:tab>`, `<a:tabLst>` — *any* element
  starting with `a:t`. On table slides it grabbed `<a:tbl>` as a text run and escaped the
  whole table into garbage → `XML_MALFORMED`. **Fix:** match `<a:t(?:\s[^>]*)?>` (a
  whitespace or `>` must follow `a:t`). v2 will do placeholder replacement the same way —
  **use the fixed pattern from day one.**

- **Dependency closure.** Copying a slide isn't enough. You must also carry:
  its **media** (images), **embeddings** (OLE/`.bin`), its **layout**, and if the layout
  belongs to a master the baseline lacks, the **whole master closure** (master + theme +
  that master's layouts + their media). Miss any and you get a dangling target.
  (`_copy_closure`, `_harvest_slide` in `harvest.py`.)

- **Content types: attribute order isn't guaranteed.** Templafy writes
  `ContentType="…" PartName="…"` (type first). Parsing that assuming PartName-first
  silently failed. Match by attribute name, not position.

- **Customer-data tags dangle.** Slides carry `<p:custDataLst><p:tags r:id=…>` pointing
  at `ppt/tags/*` parts. If you copy the slide but not the tag part → dangling; and
  several slides sharing one tag part → duplicate ref. **Fix:** strip `<p:custDataLst>`
  from copied slides (it's non-visual metadata). v2 authors clean slides, so this may not
  even arise — but strip defensively.

- **Windows `MAX_PATH` (260 chars).** Deeply nested block folders with long names blew
  past it. Keep on-disk paths short (hash long names).

- **The validator lied by omission.** It reported "OK" on malformed decks because it
  never actually parsed the XML. **Lesson: the validator must parse every part.** Ours
  now does (`xml-malformed` check).

## 3. The content model that actually held up

The single most useful realization from v1: a generated deck decomposes cleanly into:

- **Baseline** — the deck when every answer is at its default.
- **Deltas** — each answer *adds*, *removes*, or *rewords* a precise set of slides.
- **Tokens** — a few literals (client name, date) substituted as text everywhere.
- **Data-driven slides (~8%)** — fees, lead-partner names, RFP tables. These come from
  external data sources and are the irreducible remainder until those sources are wired.

v2 keeps this exact mental model. The difference: in v1 the deltas/tokens were
**inferred by comparing decks**; in v2 they are **declared by us** in the template.

- **Slide identity via `creationId`.** Templafy stamps `<a16:creationId>` GUIDs on shapes
  that survive across decks; we matched slides by that even when text differed. In v2 we
  own the slides, so identity is just "the block we authored" — simpler.

- **Tokens vs. swaps was a real subtlety.** "New York" is both a *dynamic* city value and
  *static* text on some slides; blindly replacing it corrupted static text. v2 removes
  this whole class of bug by using **explicit placeholders** (`{{City}}`) that only ever
  appear where substitution is wanted.

## 4. What v1 code is directly reusable in v2

| v1 file | Reuse in v2 |
|---|---|
| `harvest.py` → `assemble()` and its helpers | **Core.** The valid-PPTX emitter: rel-ID uniqueness, content-type overrides, dependency closure, media remap, custData strip, presentation.xml rewrite. Lift wholesale; drop the harvest half. |
| `harvest.py` → `_apply_tokens()` (fixed pattern) | Placeholder/token fill, table-safe. |
| `validate_pptx.py` | **Keep as-is.** Structural + well-formedness validator. Run on every v2 output. |
| `pptx_forensics.py` | Slide parsing / similarity helpers — useful for a "does the output match a golden file" test harness. |
| `diff_decks.py` | Regression testing: compare a v2 build to an approved golden deck. |
| FastAPI + templates scaffold (`app/`) | The template-editor UI can reuse this shell. |

**What to leave behind:** everything harvesting-specific — `align()`, `varied_fields()`,
OFAT payload generation (`make_payloads.py`), the mapper (`map_decks.py`), and any
dependency on running Templafy to generate references.
