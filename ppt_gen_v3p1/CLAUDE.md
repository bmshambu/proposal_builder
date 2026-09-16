# ppt_gen_v3p1 — rendering the built deck through Microsoft Graph

An experiment, forked from `ppt_gen_v3`. **Read this whole file before touching
anything.** It assumes you know nothing about the project it came from.

---

## 1. What this is

`ppt_gen_v3` (the sibling folder, `../ppt_gen_v3`) is a working FastAPI app that
builds PowerPoint proposals: an author imports a slide library, orders it, says
when each slide is included, binds placeholders, and anyone can then build a
deck from a JSON payload of answers. **It works.** Do not try to improve it.

This fork changes exactly one thing: **how the built deck is previewed on
screen.**

### How previews work today, and why they are being reconsidered

v3 previews a deck by copying **pages** out of a PDF of the slide library, made
once up front. It is fast and needs nothing installed — no Office, no
LibreOffice, no network — which is what makes it deployable to Azure.

It has one flaw, by construction: **the pages predate placeholder substitution,
so the cover reads `{{ClientName}}` instead of the client's name.** The preview
proves *which slides, in what order, in the real branding*. It cannot show the
values.

### What this experiment tries instead

Microsoft Graph can convert a file to PDF using Office Online's own renderer.
That converts **the built deck**, so the values *do* show, at full fidelity,
with no Office installed and from any platform.

The cost: the deck must pass through SharePoint, and it needs an Entra ID app
registration. That is the trade being evaluated here.

**This is a spike, not a migration.** The goal is a working `graph` backend
alongside the existing ones, and an honest comparison. The library-PDF path
stays exactly as it is, and stays the default, unless the results say otherwise.

---

## 2. Start by copying v3

Work on a copy, never on `../ppt_gen_v3`:

```
robocopy ..\ppt_gen_v3 . /E /XD data .venv __pycache__ .git shots
```

Keep `templates/demo/` — it is synthetic, it is what the checks run against, and
`check_api.py` needs it. Do not copy `data/`; it is generated.

`robocopy` exits **1 on success** (it means "files were copied"); anything under
8 is fine. It has not failed unless the code is 8 or more.

Then confirm the copy is sound **before changing anything**:

```
pip install -r requirements.txt
python tools/check_api.py
node tools/check_ui_render.js
```

Both must pass. If they do not, fix the copy, not the code.

---

## 3. The flow to build

```
build the deck (already works)
  └─ data/builds/<token>.pptx
       ├─ ① get an access token      POST login.microsoftonline.com/<tenant>/oauth2/v2.0/token
       │                             client credentials, scope …/.default
       ├─ ② upload the .pptx         PUT  /sites/{site}/drives/{drive}/items/root:/<path>:/content
       │                             (over ~4 MB needs createUploadSession)
       ├─ ③ convert                  GET  /drives/{drive}/items/{id}/content?format=pdf
       │                             → 302 → PDF bytes, rendered by Office Online
       ├─ ④ DELETE the item          always, in a finally — see §6
       └─ ⑤ rasterise the PDF to PNGs, locally
```

Steps ② and ④ exist because **Graph cannot convert a stream of bytes**.
`format=pdf` is an operation on a file that already lives in a drive, so the
deck has to be there first. There is no `POST /convert`. That is the awkward
heart of this design and the thing to be honest about in the write-up.

---

## 4. Where the code goes

`engine/render.py` already holds several interchangeable backends. **Add a
fourth; do not restructure.** The existing ones are the pattern to copy.

An export backend is a pair in the `BACKENDS` dict:

```python
BACKENDS = {"libreoffice": (_probe_libreoffice, _export_libreoffice),
            "powerpoint":  (_probe_powerpoint,  _export_powerpoint)}
```

- `_probe_x() -> (usable: bool, why_not: str|None)` — **cheap**, no network. For
  Graph: are the four environment variables set and is `msal` importable? Do not
  fetch a token in a probe; probes get called on ordinary page loads.
- `_export_x(pptx, out_dir, width) -> [png paths]` — raise `RenderError` on any
  failure. Callers fall back; they never crash.

Reuse what is there rather than writing it again:

- **`_rasterise(pdf, out_dir, width)`** already turns a PDF into
  `slide-001.png …` with pypdfium2. Your backend gets PDF bytes from Graph,
  writes them to a temp file, and calls it. That is most of step ⑤.
- **Caching, locking and cleanup** are handled by `deck_to_images()`, which
  wraps every backend: a per-deck lock, a `render.json` marker, and clearing a
  half-finished attempt. Your backend just exports.
- `_export_libreoffice` is the closest model — it also produces a PDF and
  rasterises it.

Then:

- add `"graph"` to `ORDER` in the position you are testing (put it **last** at
  first, so nothing changes until you ask for it explicitly),
- `engines()` already reports whatever is in `ORDER`, so `/api/renderers` picks
  it up for free,
- add `graph` to the endpoint's `engine` query pattern in `app.py`
  (`^(auto|library|libreoffice|powerpoint|svg)$` — around line 722).

`?engine=graph` must **refuse with 503 rather than falling back**, exactly as
the other named backends do. A spike that silently renders something else
teaches you nothing.

### Configuration

Environment variables only, never in code, never committed:

```
GRAPH_TENANT_ID=
GRAPH_CLIENT_ID=
GRAPH_CLIENT_SECRET=      # or a certificate; secret is fine for a spike
GRAPH_DRIVE_ID=           # the one document library this is allowed to touch
```

Add `.env` to `.gitignore` before writing a single credential anywhere.

---

## 5. Working without a tenant

Credentials may not exist yet. **Do not let that stop you**, and do not invent
progress either.

Build the backend so its HTTP calls go through one small seam, then test it
against recorded/fake responses: token, upload, a real PDF for the convert step,
delete. That proves the wiring, the error paths, the rasterising, the cache, and
the 503 refusal — everything except Graph itself.

Say plainly in every report which parts are proven against fakes and which
against the real service. When credentials arrive, run §7 for real.

---

## 6. Rules that are not negotiable

**Confidentiality.** Real templates, decks and payloads never leave the machine
and never enter git. `templates/demo/` and `fixtures/` are synthetic and safe.
For any Graph call during development, use the demo deck — never a client's.

**The uploaded copy must be deleted.** Step ④ goes in a `finally`. A confidential
proposal sitting in SharePoint because a render failed halfway is the worst
outcome this experiment can produce. Write the check that proves the item is
gone after both a success and a failure.

**Least privilege.** Ask for `Sites.Selected` scoped to one document library,
not `Files.ReadWrite.All`. If someone hands you broader credentials, say so in
the write-up rather than quietly using them.

**Never log a token, a secret or a signed URL.** Redact them in error messages
too — `RenderError` text ends up on screen.

**Determinism stays.** The deck is decided by the rules and the answers. Nothing
here may change which slides are chosen or what they say; this is a renderer.

---

## 7. What to measure, and report honestly

Against the demo library and a built deck of ~8 slides, then a larger one:

| question | why |
|---|---|
| Does it render, and do the values show? | the entire point — the library-PDF path cannot do this |
| How long, end to end, for 8 slides? For 60? | compare with 0.7 s for a 7-slide library-PDF preview |
| Does the SharePoint item always disappear? | including after a forced mid-render failure |
| What happens over ~4 MB? | simple upload stops working; is the session path needed? |
| What does Graph do under repeated calls? | throttling: 429s and `Retry-After` |
| Does SmartArt, a chart and the corporate font come out right? | the fidelity claim |
| What fails when the network is down? | must degrade to another backend, not 500 |

Write the results in `docs/graph-findings.md` in this folder: what was tested,
against real Graph or a fake, with numbers. Include what did **not** work.

The recommendation at the end should answer one question: **is showing filled
values worth a network round trip, an app registration, and a confidential file
briefly in SharePoint — or is the library-PDF preview, with its disclaimer, the
better deal?** Either answer is a good outcome. Do not argue for Graph because
the work was done.

---

## 8. House style

The code in `../ppt_gen_v3` is the reference for tone and structure. In short:

- Comments explain **why**, not what. If a line exists because something broke,
  say what broke.
- Every endpoint is a thin call into `engine/`; logic lives in the engine.
- Failures are reported with the reason, never swallowed and never fatal.
- `tools/check_api.py` runs against the real engine, not mocks — add checks
  there for anything you add, including the refusals.
- Run the full set before any commit:
  `check_api.py`, `check_fixtures.py`, `check_suggest.py`,
  `check_ui_render.js`, `check_ui_contrast.py`.
- Commit messages: what changed and **why**, wrapped at 80 columns.

---

## 9. Background worth reading, in this order

1. `../ppt_gen_v3/README.md` — what the app does and every command.
2. `../ppt_gen_v3/engine/render.py` — the module docstring explains all four
   existing renderers and why each exists.
3. `../ppt_gen_v3/docs/ge-chat-spike.md` — a related spike. Section 3 records
   what has already been learned about GE, images and hosting; the Graph
   reasoning there overlaps with this one.

A summary of what was already decided, so it is not re-litigated:

- **PowerPoint COM** renders perfectly and cannot be deployed: Azure has no
  desktop session and Microsoft does not license server-side Office automation.
- **LibreOffice** would work and is **blocked by firm policy**, on endpoints at
  least. That is what pushed previews to the library-PDF approach.
- **`engine/svg.py`** is an in-process approximation. It is fine for thumbnails
  and looks like a skeleton at full size. It is the last-resort fallback and the
  UI labels it as such.
- Graph was identified as the strongest remaining option **because it needs no
  third-party software approved anywhere** — which is exactly what this fork is
  here to find out.
