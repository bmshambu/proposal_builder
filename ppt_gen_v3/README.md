# ppt_gen_v3 — the author console

A FastAPI app over the v2 engine. Authors add a slide library, order it, say
when each slide is in, and bind placeholders. Anyone can then build a deck from
answers, deterministically.

```
pip install -r requirements.txt
uvicorn app:app --reload          # http://127.0.0.1:8000
python tools/check_api.py         # every endpoint, against the real engine
python tools/make_library_pdf.py templates/firm    # once per library, needs PowerPoint
```

Interactive API docs come free at `/docs`.

## What is here, and what is deliberately not

v3 is v2 with the harvesting removed. v2 existed to work out an undocumented
template by merging 70 generated decks and inferring the rules; that job is
finished and its tooling stays in `ppt_gen_v2/` — `build_master_deck.py`,
`propose.py`, `tokenise.py`, `identity.py`. Read them there if the firm ever
changes the template and it has to be re-harvested. Nothing in v3 imports them,
and `tools/check_api.py` would fail if something started to.

`verify.py` **did** come across. Once rules are authored by hand rather than
inferred, comparing against the decks Templafy really produced is the only
objective check that a deck is right — it stops being the mechanism and becomes
the regression net.

`load_payloads()` came with it, in `engine/payloads.py`. It was the single
function tying `verify` to the harvester, and it is small enough to own.

## Layout

```
app.py            FastAPI - thin calls into engine/, no logic of its own
engine/           the deck machinery, unchanged from v2
web/              index.html, app.css, app.js
templates/<id>/   one library: library.pptx, library.pdf, rules.json, blocks.json
data/payloads/    answer sets, for the field catalogue (gitignored)
data/builds/      built decks awaiting download (gitignored)
data/renders/     exported slide images, one folder per build (gitignored)
tools/            checks that run without a browser
```

`rules.json` on disk is the source of truth. The app is an editor for it, not a
database in front of it — every rule stays readable, diffable and hand-editable.

## Previewing without a converter

The deck viewer shows the built file slide by slide. The trick is that it never
converts anything.

A deck is assembled by copying **slides** out of the library, so its preview is
assembled by copying **pages** out of a PDF of that same library. Convert the
library once, up front:

```
python tools/make_library_pdf.py templates/firm     # needs PowerPoint, once
```

then upload the `.pptx` and the `.pdf` together on the Slides screen. After that
a preview is `pypdfium2` rasterising a handful of pages — pure Python, no Office,
no LibreOffice, no external service, and identical on a laptop and in a Linux
container. That is what makes the viewer deployable.

It holds only while **page N is slide N**, so the pairing is checked at import
and a mismatched PDF is refused rather than kept: a preview that shows a real
slide from the real library, just not the one in the deck, is worse than no
preview. Hidden slides are the usual cause — PowerPoint leaves them out of a PDF
export — and `make_library_pdf.py` names them before it converts.

**What it cannot do is show filled values.** The pages predate substitution, so
the cover reads `{{ClientName}}`. The viewer says so under the filmstrip, every
time. It answers *which slides, in what order, in the real branding*; the Values
screen and the build's own report answer whether the values landed.

Three fallbacks sit behind it, and `/api/renderers` says which are usable here:
`libreoffice` if it is installed, `powerpoint` for an exact local check before a
deck goes out, and `svg` — `engine/svg.py`, always available, an approximation
that the viewer labels in warning colour so nobody mistakes it for the deck.
`engine/render.py` never quits a PowerPoint it did not start, so an export
cannot close an author's own unsaved work.

## The one list

The field catalogue in `engine/payloads.py` fills **both** the build form and
the condition editor. A form and a rule therefore cannot disagree about a
field's name or which values it takes. Typing a field name by hand is how a
rule silently stops matching; the UI never offers the chance.

## Saving is an explicit overwrite

An author editing a template is usually *updating* rules that already exist —
the case a prototype quietly designs away. So `PUT /api/libraries/{lib}/rules`:

- validates against the library and **refuses before writing**, not after,
- backs up the previous `rules.json`, timestamped, next to it,
- returns **what changed** (added, removed, conditions, reordered) rather than
  just "saved".

`tools/check_api.py` asserts that a rejected save leaves the working file
intact, because that is the worst outcome this app has available.

## Not yet true

Stated plainly so nobody assumes otherwise:

- **No authentication.** The author/user split is a convention, not a control.
  Anyone who can reach the app can edit rules for every library.
- **`data/builds/` is never cleaned up.** It grows until someone empties
  it, and `data/renders/` holds a folder of images beside each build.
- **One library's rules, one writer.** There is a lock, but no version check
  across browser tabs.

All three are fine for a single author on localhost and are the first things to
fix before anyone else can reach it.

## Confidentiality

Templates, generated decks and payloads are confidential and stay out of git —
see `.gitignore`. `templates/demo/` is synthetic and safe to commit; it is what
the checks run against. Everything runs on this machine and nothing is sent
anywhere else. Keep that true when this moves off localhost.
