# Authoring a library deck

The point of v2 is that a template is **a normal PowerPoint deck plus two
conventions**. Designers keep working in PowerPoint; nothing about their tooling
changes. This page is what you hand them.

Adding a template is a **file operation, never a code change**. There are two
routes in:

| | Route | Use when |
|---|---|---|
| A | `build.py import <deck.pptx>` — the deck is copied untouched and named from its slide titles | you already have a firm template |
| B | Author with `{{block:id}}` markers, as below | you own the deck and want ids that travel with the slides |

Route A is covered in §0; §§1–6 describe route B and apply to both once the
template exists.

---

## 0. Importing a deck you already have

```bash
python build.py import "Firm Proposal Template.pptx" --name firm
python build.py inspect firm
```

That copies the deck **byte for byte** into `templates/firm/`, derives a block id
per slide from its title, records those ids in `blocks.json`, and writes a starter
`rules.json` whose baseline is every slide in the deck's own order. It builds as-is
— a faithful copy of the source deck — and you narrow it from there.

Ids that read badly are fixed without opening PowerPoint:

```bash
python build.py rename firm scope_of_work_expanded scope_expansion
```

`rename` updates `blocks.json` and `rules.json` together, so the two never drift.

The one thing import cannot do is invent placeholders. Where a value belongs on a
slide, someone still types `{{ClientName}}` into it in PowerPoint (§3) — and that
is deliberate: guessing which text is dynamic is exactly the v1 problem v2 exists
to delete.

---

## 1. One deck, one block per slide

Make the branded slides you want the engine to be able to use — cover, about us,
scope, fees, contacts, plus any optional sections. Save it as `library.pptx`
inside a template folder:

```
templates/<template-name>/
  template.json       name, description, which file is the library
  library.pptx        the deck itself
  blocks.json         slide -> block id (written by import; optional if you use markers)
  rules.json          which blocks a given answer set produces
  data_sources.json   stubs for external systems (optional)
```

Keep **one** library per template. One deck means one set of masters, layouts and
theme, so a block can never be branded against the wrong master — that whole class
of drift is prevented by construction rather than checked for.

## 2. Name each slide with a block marker

> Skip this if you imported the deck — `blocks.json` already names every slide, and
> a marker is only worth adding when you want the id to survive being copied into a
> different deck.


Put a small text box on every slide containing exactly:

```
{{block:cover}}
```

Use a short, stable id: lower case, words joined by `_`. **Park the box off the
side of the canvas** so it never shows while editing or presenting. The builder
deletes the whole shape from the finished deck, so it never ships either.

Why a marker and not the slide name: it is explicit, it survives reordering and
copy/paste between decks, and it is visible to whoever is editing the library.
(This settles open decision 1 in `v2-plan.md` §7.)

An unmarked slide still works. Its id is resolved in this order, first hit wins:

1. a `{{block:id}}` marker on the slide,
2. its entry in the `blocks.json` sidecar,
3. its slide title, slugified,
4. its position (`slide7`).

`build.py inspect <template>` shows which of those each block is resting on and
flags 3 and 4 with `?`, because reordering the deck renames them. It also warns
when a slide has been retitled since import, since that usually means the recorded
id no longer fits.

**Block ids must be unique.** Two slides with the same marker is an error, not a
last-one-wins.

## 3. Type placeholders where values belong

Anywhere a value should be substituted, type:

```
{{ClientName}}     {{DueDate}}     {{City}}     {{LeadPartner}}
```

Rules:

- Letters, digits, `_` and `.` in the name. `{{Client.City}}` is fine.
- **Type it in one go.** PowerPoint splits text into runs when you edit it
  character-by-character, and a split placeholder is invisible to a naive
  replacer. The builder stitches split runs back together before substituting —
  the demo library deliberately contains a placeholder split across four runs to
  keep that guard tested — but typing it whole keeps the file tidy.
- A placeholder that is **not** bound in `rules.json` is left visible on the
  slide and reported by the build, so a gap is obvious rather than silently blank.
- Static text that merely *looks* dynamic is safe. Substitution happens only where
  a `{{…}}` appears — this is why v2 has no equivalent of v1's "is 'New York' a
  city value or just the word?" problem.

Placeholders work in tables too.

## 4. Check it before writing rules

```bash
python build.py inspect <template-name>
```

That lists every block, the slide it came from, and the placeholders on it, then
checks the rules against the library: blocks the rules reference but the library
lacks, placeholders on slides that nothing binds, bindings nothing uses.

## 5. Write the rules

`rules.json` is the other half of the template (full shape in `v2-plan.md` §3):

```jsonc
{
  "baseline": ["cover", "about_us", "approach", "scope", "fees"],  // the spine, in order
  "blocks": {
    "scope": {
      "slides": ["scope"],
      "when": "always",
      "variant": {                        // same position, different content
        "when": { "field": "AuditType", "eq": "Expansion of Services" },
        "slides": ["scope_expansion"]
      }
    },
    "expansion_detail": {                 // not in the baseline: conditional
      "slides": ["expansion_detail"],
      "when": { "field": "AuditType", "eq": "Expansion of Services" },
      "insert_after": "approach"
    }
  },
  "placeholders": {
    "{{ClientName}}":  { "from": "field", "field": "FullClientName" },
    "{{DueDate}}":     { "from": "field", "field": "DueDate", "format": "long_comma" },
    "{{LeadPartner}}": { "from": "data",  "source": "profile", "key": "lead_partner" }
  }
}
```

**Conditions:** `"always"`, `"never"`, `{field, eq|ne|in|not_in|exists}`, and
`{"all": […]}` / `{"any": […]}` / `{"not": …}`. Values compare as strings,
case- and whitespace-insensitively, so an answer of `" expansion of services "`
still matches.

**Ordering:** `baseline` is the spine. A block that isn't in the baseline names
where it slots in with `insert_after`. A block with no anchor is appended.

**Bindings:** `from` is `field` (an answer), `data` (an external source, wired in
Milestone 4 — anything unwired is reported by name), or `literal`. `format`
accepts `upper` / `lower` / `title` and the date formats in
`engine/bindings.py` (`long_comma`, `day_month`, `iso`, …).

## 6. Build

```bash
python build.py make <template-name> --answers <answers>.json \
                --out out/deck.pptx -v
```

Add `--strict` in CI: it refuses to write a deck that still has an unfilled
placeholder.

---

## Things that will bite

| Symptom | Cause | Fix |
|---|---|---|
| `duplicate block id` | two slides carry the same marker, or the sidecar names one id twice | rename one |
| a block shows as `slide7` | the slide has no marker, no sidecar entry and no title | `rename` it, or add a marker |
| a block id reads oddly | it was guessed from the slide's title at import | `build.py rename <template> <old> <new>` |
| `title was …, now …` | the slide was retitled after import | check the id still fits; `rename` if not |
| `{{Foo}}` visible in the output | not bound in `rules.json` | add a binding, or delete the placeholder |
| a bound placeholder does nothing | no slide uses it | `inspect` lists these as unused |
| `rules reference block 'x'` | the rules name a block the library lacks | fix the id, or add the slide |

## Not supported yet

- **Speaker notes are dropped.** A notes part back-references its slide, and a
  block used twice would leave two slides pointing at one notes part. The build
  reports how many were dropped.
- **One master library per template.** Per-block `.pptx` fragments are deferred
  (open decision 2) until single-deck editing actually gets unwieldy.
