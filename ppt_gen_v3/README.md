# ppt_gen_v3 — the author console

A FastAPI app over the v2 engine. Authors add a slide library, order it, say
when each slide is in, and bind placeholders. Anyone can then build a deck from
answers, deterministically — the same answers always give the same deck.

Every command below runs **from this folder** (`ppt_gen_v3/`), and each is one
line so it pastes straight into PowerShell or cmd.

---

## Setup

```
pip install -r requirements.txt
uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. Interactive API docs are at `/docs`.

`pywin32` in the requirements is Windows-only and optional; it is needed only
for `tools/make_library_pdf.py`. The app itself never uses it.

---

## Adding a library and authoring its rules

The console walks the author through four screens, in order. Build a deck is a
fifth screen, for users rather than authors.

### 0 · Make the library PDF — once per library, before importing

```
python tools/make_library_pdf.py path\to\library.pptx
```

Writes `library.pdf` next to the `.pptx`. Needs PowerPoint, which is why it is
a script and not a button: the deployed app must never need it. Use `-o` to
write somewhere else.

It refuses if any slide is **hidden** — PowerPoint leaves hidden slides out of a
PDF, and one of them would shift every page after it onto the wrong slide. It
names them; unhide them or remove them from the library. `--force` converts
anyway, for looking at only, never for uploading.

It finishes by printing the page → block table. Glance down it.

### 1 · Slides — import the library

Type a name (lower case, digits, underscores — it becomes the folder under
`templates/` and the id every rule refers to, so choose it once). Drop the
`.pptx` **and** its `.pdf` on the drop zone, in either order, then **Import
library**.

The `.pdf` is optional but the preview depends on it. Without one, import still
succeeds and says so; the viewer falls back to an approximation.

Re-importing under an existing name replaces the library **and its rules**, and
asks first.

### 2 · Questions — upload answer sets

Drop the `.json` answer sets (the payloads Templafy was given). They are kept
under `data/payloads/<library>/` and become the **field catalogue**: the build
form and the condition dropdowns on the Rules screen both come from it.

Upload several that differ. A field that has the same value in every answer set
cannot be a condition, and the screen tells you which those are.

Adding more later **adds**; re-uploading a file with the same name **replaces**
that one.

### 3 · Rules — order, and when each slide is in

Drag rows into deck order. Give each slide a condition, or leave it `always`.
Drag a slide out of the deck to drop it entirely.

**Suggest from decks** reads decks Templafy produced and proposes the order and
conditions, as markdown to read against the screen — nothing is applied for you.
When rules already exist it reports only **what changed**, so a second batch of
decks surfaces the new things rather than restating everything settled.

The same suggester runs from the command line:

```
python tools/suggest_rules.py --library templates/<library> --decks ..\data\decks --payloads ..\data\payloads
```

`--decks` takes files, folders or globs, so trying a subset is normal. `--all`
forces the full walkthrough even when rules exist. `--json out.json` also writes
a suggested `rules.json` to read — it does not replace yours.

**Save rules** validates first and refuses rather than writing something that
cannot build. Every save backs up the previous `rules.json`, timestamped.
**Earlier versions** lists those backups and restores any of them. **Start the
rules over** clears every condition back to `always` in library order — for
when you import a different library and want a clean slate. Your placeholder
bindings are kept, and the old rules are backed up first, so it can be undone
from Earlier versions.

### 4 · Values — bind each placeholder

Point each `{{Placeholder}}` at an **answer field**, a **fixed value** typed
here, or a **data source**. A field holding a date can be given a display format
— the preview beside it shows exactly what the deck will print. The screen has
its own **Save**, so you do not have to go back to Rules.

A binding to a field that no answer set contains is reported — otherwise the
deck would ship with `{{ClientName}}` printed on the cover and nothing said.

### Check against Templafy

The **Check against Templafy** button in the header. Drop Templafy's deck and
ours; it says whether the slides, their order and their text match, and names
the slide to fix when they do not.

To check many decks at once, build each from its answer set and compare:

```
python tools/verify_decks.py --library templates/<library> --decks ..\data\decks --payloads ..\data\payloads
```

Decks and answer sets pair by name — `00_baseline.pptx` with
`00_baseline.json`. `--limit 10` does the first ten, for a quick look. Run it
after changing rules, before trusting them.

| verdict | meaning | where to fix |
|---|---|---|
| `MATCH` | right slides, order and text | — |
| `SELECTION OK, TEXT DIFFERS` | right slides and order, some text differs | Values — usually a binding or date format |
| `SELECTION OK - N data-driven slide(s)` | N slides Templafy regenerates per deck could not be paired | usually nothing |
| `ORDER DIFFERS` | right slides, wrong order | Rules — drag |
| `SELECTION DIFFERS` | a slide missing or extra | Rules — a condition |
| `BUILD FAILED` | the rules could not build the deck | the error printed beside it |

One known text difference is not ours: Templafy's own decks carry an unfilled
`{{ Form.ShortClientName }}` on one slide. Filling it correctly shows up as a
difference there, and that is the right outcome.

---

## Building a deck

**Build a deck** is the only screen a user needs. Fill the form, or drop an
answer set to fill it for you — the file fills the form rather than bypassing
it, and does not change the library's questions. **What you will get** updates
as you answer. **Build the deck**, then **Download .pptx**.

The preview takes the right-hand column once a deck is built. Arrow keys move
between slides (not while typing in a field).

**The preview shows placeholders unfilled.** It is cut from the library's own
pages, so the cover reads `{{ClientName}}`. That is expected and the screen says
so every time: the preview confirms which slides and in what order; the filled
values are in the downloaded `.pptx`.

---

## Command reference

| command | what it does | when |
|---|---|---|
| `uvicorn app:app --reload` | run the console | always |
| `python tools/make_library_pdf.py <library.pptx>` | make the preview PDF; refuses on hidden slides | once per library, before import |
| `python tools/suggest_rules.py --library … --decks … --payloads …` | suggest order and conditions from real decks | authoring rules |
| `python tools/verify_decks.py --library … --decks … --payloads …` | build every answer set, compare with Templafy's deck | after changing rules |
| `python -m engine.validate <deck.pptx>` | will PowerPoint open this, or offer to repair? | a deck that misbehaves |
| `python tools/shoot_ui.py` | photograph the build screen at two widths, into `tools/shots/` | after a layout change |
| `python tools/make_test_fixtures.py` | regenerate `fixtures/` | only when changing the fixtures |

### The checks

Run all of them before committing:

```
python tools/check_api.py
python tools/check_fixtures.py
python tools/check_suggest.py
node tools/check_ui_render.js
python tools/check_ui_contrast.py
```

| check | proves |
|---|---|
| `check_api.py` | every endpoint against the real engine, including the refusals, and that a preview slide is byte for byte the library page the rules chose |
| `check_fixtures.py` | the whole author exercise end to end through the API: import, rules, build three decks, `MATCH` each |
| `check_suggest.py` | the suggester recovers the known answer: 15 of 15 conditions |
| `check_ui_render.js` | the page boots against a strict DOM stub and every region renders |
| `check_ui_contrast.py` | every text/background pair clears WCAG contrast in both themes |

`check_ui_render.js` cannot see layout. `shoot_ui.py` exists because a layout
bug passed every check here; open the pictures after changing CSS.

On a Windows console, `check_suggest.py` may need `set PYTHONIOENCODING=utf-8`
first — its report contains characters cp1252 cannot print. The check itself is
unaffected.

### Trying it end to end without a confidential file

`fixtures/` holds a 15-slide master, three answer sets and the deck each should
produce. `fixtures/README.md` walks through it: import `master.pptx`, author the
rules, build, and compare against `fixtures/targets/`. The goal is `MATCH` on
all three, and `fixtures/reference_rules.json` is the answer key.

---

## When something looks wrong

| you see | why | do |
|---|---|---|
| `{{ClientName}}` in the preview | the preview is the library's own pages | nothing — check the downloaded `.pptx` |
| `{{ClientName}}` in the **downloaded** deck | the placeholder is unbound, or bound to a field the answers lack | Values screen; the build warns in red |
| preview labelled *approximation* in orange | the library has no PDF | make one, re-import with it |
| *the PDF has N page(s) and the library has M* | hidden slides, or a PDF of a different deck | `make_library_pdf.py` names hidden slides |
| *this library has no rules yet* | nothing saved on the Rules screen | author and save rules |
| a condition never matches | the value was typed rather than picked | pick from the dropdown — the values come from the answer sets |
| a field missing from the dropdowns | no uploaded answer set contains it | upload one that does, on Questions |
| deck has the wrong slide count | a condition, or a slide left in or out | `verify_decks.py` names the slide |

---

## How it works

### What is here, and what is deliberately not

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

### Layout

```
app.py                  FastAPI - thin calls into engine/, no logic of its own
engine/                 the deck machinery
web/                    index.html, app.css, app.js
templates/<id>/         one library: library.pptx, library.pdf, rules.json, blocks.json
data/payloads/<id>/     that library's answer sets (gitignored)
data/builds/            built decks awaiting download (gitignored)
data/renders/           preview images, one folder per build (gitignored)
fixtures/               a synthetic library to practise on
tools/                  scripts and checks
```

`rules.json` on disk is the source of truth. The app is an editor for it, not a
database in front of it — every rule stays readable, diffable and hand-editable.

### Previewing without a converter

A deck is assembled by copying **slides** out of the library, so its preview is
assembled by copying **pages** out of a PDF of that same library. After the
one-time conversion, a preview is `pypdfium2` rasterising a handful of pages —
pure Python, no Office, no LibreOffice, no external service, identical on a
laptop and in a Linux container. That is what makes the viewer deployable.

It holds only while **page N is slide N**, so the pairing is checked at import
and a mismatched PDF is refused rather than kept: a preview that shows a real
slide from the real library, just not the one in the deck, is worse than no
preview.

The mapping cannot be recovered from the built file — once placeholders are
filled, a block named after its title has a different id there. So each build
records which library pages it used, at the moment the selection is in hand.

Three fallbacks sit behind it, and `/api/renderers` says which are usable on a
given machine: `libreoffice` if it is installed, `powerpoint` for an exact local
check, and `svg` — `engine/svg.py`, always available, an approximation that the
viewer labels in warning colour so nobody mistakes it for the deck.
`engine/render.py` never quits a PowerPoint it did not start, so an export
cannot close an author's own unsaved work.

### The one list

The field catalogue in `engine/payloads.py` fills **both** the build form and
the condition editor. A form and a rule therefore cannot disagree about a
field's name or which values it takes. Typing a field name by hand is how a
rule silently stops matching; the UI never offers the chance.

### Saving is an explicit overwrite

An author editing a template is usually *updating* rules that already exist —
the case a prototype quietly designs away. So `PUT /api/libraries/{lib}/rules`:

- validates against the library and **refuses before writing**, not after,
- backs up the previous `rules.json`, timestamped, next to it,
- returns **what changed** (added, removed, conditions, reordered) rather than
  just "saved".

`tools/check_api.py` asserts that a rejected save leaves the working file
intact, because that is the worst outcome this app has available.

---

## Not yet true

Stated plainly so nobody assumes otherwise:

- **No authentication.** The author/user split is a convention, not a control.
  Anyone who can reach the app can edit rules for every library.
- **`data/builds/` is never cleaned up.** It grows until someone empties it,
  and `data/renders/` holds a folder of images beside each build.
- **One library's rules, one writer.** There is a lock, but no version check
  across browser tabs.
- **Storage is the local filesystem.** Rules, libraries and answer sets are
  files next to the app. Moving to Azure means choosing where they live instead.

All four are fine for a single author on localhost and are the first things to
fix before anyone else can reach it.

## Confidentiality

Templates, generated decks and payloads are confidential and stay out of git —
see `.gitignore`. `templates/demo/` and `fixtures/` are synthetic and safe to
commit; they are what the checks run against. Everything runs on this machine
and nothing is sent anywhere else. Keep that true when this moves off
localhost.
