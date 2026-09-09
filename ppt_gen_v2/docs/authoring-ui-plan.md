# Authoring rules in a UI, not inferring them from Templafy

Status: plan, nothing built yet. Supersedes the "infer rules by diffing 70
generated decks" approach as the *primary* path.

## The pivot

Until now the rules were **inferred**: merge 70 Templafy decks into a library,
work out from the payloads why each slide was there, and write `rules.json` from
that evidence. It got selection to 70/70 and text to 0 differences, and it was
the right way to *harvest* a template nobody had documented.

It is the wrong way to *own* one. Inference has a ceiling we have now hit twice:

- Three industry rules are explained equally well by two different answers. No
  amount of evidence separates them, because the 70 decks genuinely do not.
- Order came out one slide wrong on every deck, and the cause was a merge
  artifact nobody could see by reading the library.

Both are questions only the template's author can answer. The plan is to ask
them, in a UI, instead of guessing and then correcting the guess.

**What the author owns after this:** which slides are in the deck, in what
order, under what condition, and what every placeholder is filled from.
**What the tool owns:** showing them the slides, catching contradictions, and
building the deck deterministically.

## What we keep

Almost everything. This is a new front end onto the engine that exists, not a
rewrite.

| Piece | Role after the pivot |
|---|---|
| `library.pptx` | Unchanged. The hard-won artifact: every unique slide, placeholders restored. |
| `engine/library.py` - `inspect` | Step 1 of the author's flow, as-is. |
| `engine/svg.py` - `render_template()` | Slide thumbnails **in-process, no LibreOffice**. This is what makes the UI possible. |
| `engine/rules.py` - `select()` | Unchanged. See the finding below. |
| `engine/assemble.py`, `bindings.py` | Unchanged. |
| `engine/validate.py`, `check` | Runs before every save, so a broken `rules.json` is never written. |
| `engine/verify.py` | **Demoted but kept** - see "The risk" below. |

## What we stop doing

`build.py rules --decks` stops being the way rules become correct. `propose.py`
stays, and stays useful, as a **first draft**: it can pre-fill the ordered list
and pre-tick the obvious conditions so the author starts from something rather
than 128 blank rows. Its output is a suggestion the author confirms, which is
what its own docstring always said it was.

Being straight about it: yesterday's consensus-ordering fix matters much less
now. If the author sets the order by hand, a merge artifact in the library order
is something they fix by dragging a row. The fix is still correct and still
makes the pre-filled draft better, but it is no longer load-bearing.

## The finding that shapes the UI

The engine **already supports the model a UI wants**, and I verified it:

```
baseline: [cover, about, expansion, scope, fees]     # one ordered list
blocks:   expansion -> when AuditType = "Expansion of Services"
                                                     # no insert_after anywhere

AuditType = Recurring              -> cover, about, scope, fees
AuditType = Expansion of Services  -> cover, about, expansion, scope, fees
```

`select()` walks `baseline` in order and drops any block whose own `when` is
false. So **every block can live in the ordered list and carry its own
condition**, and `insert_after` is never needed.

That matters more than it looks. `insert_after` exists because *inference* has
to describe a position it did not observe directly ("this slide follows that
one"). An author does not need it - they can see the position, because it is a
row in a list. So the UI writes:

- one ordered list = the deck at "everything on",
- one condition per row = when that slide is in or out.

Same `rules.json` schema, same engine, **no selection code changes**. A rules
file written by the UI is still readable, still diffable, still hand-editable.

`variant` (swap a slide's content without moving it) stays available but is not
in the first UI; it is a second-order feature and only one demo block uses it.

## The screens

### 1. Library - "what have I got?"

Upload `library.pptx` -> `import_deck()` makes the template folder -> `inspect`.

Shows a contact sheet of every block: thumbnail, block id, source of that id
(`marker` / `title` / `position`), and the placeholders on it.

Warnings surfaced here, all of which `inspect` already computes:

- blocks with no stable id (named by title or position - reordering renames
  them), with a rename box,
- `map_drift` - the sidecar and the deck disagree,
- **literals that look like values but are not placeholders.** Tokenising was
  deliberately conservative and skipped every `mixed`/`static` case, so some
  slides may still contain a real client name as plain text. Shipping that is a
  visible embarrassment, so it belongs on the first screen, not in a log.

### 2. Rules - "what goes in the deck?"

Two panes.

**Left: the library pool** - every block not currently in the deck, as
thumbnails. Drag into the deck. This is the answer to "the author should be
aware of the slides": an unused slide is visibly sitting there.

**Right: the deck order** - the ordered list, drag to reorder. Each row:

```
[thumb]  fees_expansion                      [ always                                 v ]
[thumb]  peer_review_detail                  [ AuditType = "Expansion of Services"    v ]
```

The condition editor is a **dropdown, never a text box**: field, operator,
value. All three are populated from the field catalogue (below), so a rule
cannot fail silently because someone typed `AuditTyp`. Operators are the ones
the engine already has - `eq / ne / in / not_in / exists`, combined with
`all / any / not`. Start with a single clause plus "add clause".

**Test-drive strip along the bottom** - pick a payload, or set answers with the
same widgets, and the deck list updates live: rows that drop out grey off.
`select()` is pure and fast, so this is instant, and it is what actually makes
an author *understand* their own rules. This is the feature most likely to be
underestimated; it should not be cut.

### 3. Mapping - "where does each value come from?"

You asked how to do this one. The proposal:

**Get the field catalogue from a sample payload, not from typing.** The author
uploads one payload JSON (they already have 70); we flatten it to dot-paths and
that *is* the list of fields, each shown with its example value. Load several
payloads and each field also gets its set of observed values - which is exactly
what the condition dropdowns need too. One upload feeds both screens.

Then the screen is two columns, left driven entirely by the library:

```
{{ClientName}}   used on 12 slides  ->  field   v  FullClientName v  "Example Corporation"
{{DueDate}}      used on 3 slides   ->  field   v  DueDate        v  [November 30, 2026 v]
{{LeadPartner}}  used on 2 slides   ->  data    v  profile / lead_partner      (!) not wired
{{FirmName}}     used on 1 slide    ->  literal v  "Demo LLP"
```

- The three `from` kinds are the ones `bindings.py` already resolves -
  `field`, `data`, `literal`. No new binding types.
- **Dates:** when the chosen field parses as a date, the format dropdown renders
  the author's *actual* sample value in all eight formats
  (`November 30, 2026`, `30/11/2026`, ...) and they pick the one that looks
  right. `bindings.date_formats()` already produces exactly this list. Picking a
  format by seeing it beats naming it.
- **`data` bindings** are the ~8% that need an external system (fees, partner
  names). They stay declarable and are shown as *unwired* rather than hidden, so
  the remaining integration work is a visible list instead of a discovery.
- Clicking a placeholder highlights the slides that use it.
- Live status from `inspect`: in the library but unbound = red (the deck will
  ship with `{{...}}` visible); bound but unused = grey (harmless, probably a
  leftover).

## What the real payloads changed

The first mockup used tidy field names - `AuditType`, `City` - and looked fine.
The real payloads have 20 fields and do not look like that:

| Real | The mockup had assumed |
|---|---|
| field names up to **88 characters**, because they are whole form questions | short identifiers |
| one field name **with spaces** in it | identifier-shaped names |
| **31** sub-sectors, **21** cities, **11** sectors | a menu of four |
| booleans are real JSON `false` | the strings `"true"`/`"false"` |
| one field **never varies** (always `"Accept"`) | every field is a usable condition |

The model was fine throughout - `flatten()` handles all of it, and `_same()`
already compares `False` to `"false"` correctly. It was the *screen* that broke.
So the mockup now:

- lets the condition row take the remaining width and truncates the **label**,
  never the value: `Expansion of Services` is the part that says what a rule
  does, and the full question stays in the tooltip and in the editor,
- shows the exact field name in the editor, in monospace, so there is no doubt
  what gets written to `rules.json`,
- uses a type-to-filter list once a field has more than 12 known values,
- writes real booleans rather than the string `"true"`,
- refuses to pretend a field that never varies is a usable condition, matching
  what `propose.py` already refuses to infer from.

`tools/make_ui_catalogue.py` builds this from the payloads into
`ui/catalogue.js`, which is **gitignored** - the payloads are confidential. The
mockup falls back to synthetic data when the file is absent, so a fresh clone
still opens.

## The risk, stated plainly

Inference had an oracle: 70 decks Templafy really produced. Hand-authored rules
have none. A wrong rule is now invisible until a human looks at a deck.

Three mitigations, in order of value:

1. **Keep `verify` and put a button on it.** We still have the 70 decks and
   their payloads. After any rules change, "check against Templafy" is a
   one-click regression test that is free now that it exists. It stops being the
   *mechanism* and becomes the *safety net*, which is a better job for it.
2. **The test-drive strip.** Catches the author's own misunderstanding at the
   moment they introduce it.
3. **`check` before every save**, plus a timestamped backup of `rules.json` on
   each write. Never write a `rules.json` that fails validation.

## Architecture

- **Stdlib `http.server`, no framework.** The project has zero third-party
  dependencies today, everything runs locally, and nothing leaves the machine.
  Adding Flask to serve one local page trades that for very little. Revisit if
  the app outgrows it.
- JSON API over the engine + static HTML/JS/CSS. No build step.
- The engine stays import-only and path-based; the web layer holds no logic that
  `build.py` cannot also reach. Everything the UI does stays doable from the CLI.
- `rules.json` on disk stays the source of truth. The UI is an editor for it,
  not a database in front of it.

### Deferred, and why it is safe to defer

Single author on localhost for now, which makes these non-issues *today* and
blockers the moment it is shared:

- unique output paths per build (two builds currently collide),
- atomic template import (a failed upload leaves a half-template),
- background jobs for `verify` (70 decks is minutes, not milliseconds),
- roles/auth - the author/user split from the original workflow is currently a
  convention, not a control.

## Milestones

**M1 - Serve the library.** `http.server` app, upload `.pptx` -> `import_deck()`
-> library screen with thumbnails, ids, placeholders, warnings. Read-only.
*Done when an author can upload `library.pptx` and see all 128 slides.*

**M2 - Edit the order.** Deck list + pool, drag to reorder, save to `rules.json`
via `check`, with backup. No conditions yet - every row `always`.
*Done when reordering in the UI changes the built deck.*

**M3 - Conditions + field catalogue.** Payload upload -> catalogue; condition
dropdowns; test-drive strip.
*Done when an author can express "this slide only when AuditType is X" and see
it drop out live.*

**M4 - Mapping screen.** Placeholder -> binding, with the date-format preview
and unwired-`data` list.
*Done when a deck built from the UI has no visible `{{...}}`.*

**M5 - Regression button.** `verify` against the 70 decks, run in the
background, results in the UI.
*Done when an author can change a rule and see whether it broke any of the 70.*

**M6 - Draft from evidence.** Wire `propose.py` in as "pre-fill from the decks"
so the author starts from a draft. Deliberately last: the UI must be usable by
an author who has no generated decks at all, or we have rebuilt the dependency
we are trying to remove.

## Decided

**A slide never appears twice in one deck** (author, 2026-09-10). So the deck
order is a set, not a bag: a block id occurs at most once in the ordered list.
That is what the engine already assumes, and it is what keeps the UI simple -
"in the deck or not" is one yes/no per slide, the pool and the deck are two
halves of one list, and a drag is a move rather than a copy. Had the answer gone
the other way, every row would have needed its own instance id.

## Open questions for the author

1. **Do conditions ever need more than one field?** The engine has
   `all`/`any`/`not` already; the question is whether the *UI* needs to expose
   them in M3 or can wait.
2. **Who supplies the field catalogue in production?** Uploading a sample
   payload works for us because we have 70. A real deployment probably has a
   form definition somewhere that would be a better source.
