"""The author console, as a FastAPI app.

    pip install -r requirements.txt
    uvicorn app:app --reload            ->  http://127.0.0.1:8000

The web layer holds no logic. Every endpoint is a thin call into `engine/`, so
anything the UI can do is also doable from a Python prompt, and a bug lives in
one place rather than two. `rules.json` on disk stays the source of truth: this
is an editor for it, not a database in front of it.

Confidentiality is a design constraint, not a deployment detail. Templates and
client decks never leave the machine this runs on: uploads go to `templates/`
and `data/`, builds are written to disk and handed back, and nothing is sent
anywhere else. Keep it that way when this moves off localhost.

What is deliberately *not* here yet, so nobody assumes it is:

  - **No authentication.** The author/user split in the workflow is a
    convention right now, not a control. Anyone who can reach this can edit
    rules for every library.
  - **Builds are not cleaned up.** `data/builds/` grows until someone empties
    it, and `data/renders/` holds a folder of slide images beside each one.

Both are fine for a single author on localhost and are the first things to fix
before more than one person can reach it.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine import (Rules, Template, build_template,               # noqa: E402
                    find_template, import_deck, list_templates)
from engine import bindings                                        # noqa: E402
from engine.rules import flatten                                   # noqa: E402
from engine import payloads as payloadsmod                         # noqa: E402
from engine import render as rendermod                             # noqa: E402
from engine import svg as svgmod                                   # noqa: E402
from engine.template import TemplateError                          # noqa: E402
from engine.verify import block_names, compare                     # noqa: E402

WEB = os.path.join(HERE, "web")
TEMPLATES = os.path.join(HERE, "templates")
PAYLOADS = os.path.join(HERE, "data", "payloads")
BUILDS = os.path.join(HERE, "data", "builds")
RENDERS = os.path.join(HERE, "data", "renders")
for _d in (TEMPLATES, PAYLOADS, BUILDS, RENDERS):
    os.makedirs(_d, exist_ok=True)

# One writer at a time. A single author cannot really collide with themselves,
# but a half-written rules.json is unrecoverable and a lock is two lines, so
# the cheap version of the guarantee goes in now rather than after an incident.
WRITE_LOCK = threading.Lock()

MAX_UPLOAD = 200 * 1024 * 1024        # a real firm library ran to 41MB
NAME_RE = re.compile(r"[a-z0-9_]{1,64}")

app = FastAPI(title="Proposal builder",
              description="Author slide libraries and the rules that select "
                          "from them; build decks deterministically.",
              version="3.0.0")


# --------------------------------------------------------------- contracts
class DeckRow(BaseModel):
    id: str
    when: Optional[Any] = None            # null == always; else a rules.json condition


class SaveRules(BaseModel):
    deck: List[DeckRow]
    placeholders: Optional[Dict[str, Any]] = None


class ResetRules(BaseModel):
    # Bindings are about values, not slide selection, and rebuilding them by
    # hand is the expensive half. Keeping them is the safe default; clearing
    # them has to be asked for.
    keep_bindings: bool = True


class RestoreRules(BaseModel):
    backup: str


class Answers(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)


class SavePayload(BaseModel):
    name: str
    answers: Dict[str, Any]


# ----------------------------------------------------------------- helpers
def library(lib: str) -> Template:
    """Path-parameter dependency: a library id resolves to a Template, or 404."""
    if not NAME_RE.fullmatch(lib):
        raise HTTPException(400, "not a library id: %r" % lib)
    try:
        return find_template(TEMPLATES, lib)
    except TemplateError as exc:
        raise HTTPException(404, str(exc))


def describe(tpl: Template) -> dict:
    d = tpl.describe()
    return {"id": os.path.basename(tpl.folder), "name": tpl.name,
            "description": d.get("description") or "",
            "slides": d.get("blocks", 0), "unnamed": d.get("unnamed", 0),
            "answer_sets": d.get("answer_sets", []),
            "real": "error" not in d, "error": d.get("error")}


def inspect(tpl: Template) -> dict:
    """Everything the Library screen shows, in one call.

    One request on purpose: the screen is useless in pieces, and an author
    watching three spinners resolve separately learns nothing extra.
    """
    with tpl.open_library() as lib:
        blocks = [{"id": b.id, "title": b.title, "source": b.source,
                   "part": os.path.basename(b.part),
                   "placeholders": sorted(b.placeholders)}
                  for b in lib.ordered()]
        all_ph = sorted(lib.all_placeholders())
        drift = list(lib.map_drift())
        problems, bound = [], set()
        if os.path.exists(tpl.rules_path):
            rules = Rules.load(tpl.rules_path)
            problems = rules.check_against(lib)
            bound = {p.strip("{} ") for p in rules.placeholders}

    by_source: Dict[str, int] = {}
    for b in blocks:
        by_source[b["source"]] = by_source.get(b["source"], 0) + 1

    return {"library": describe(tpl), "blocks": blocks, "placeholders": all_ph,
            "unbound": sorted(set(all_ph) - bound),
            "unused": sorted(bound - set(all_ph)),
            "by_source": by_source, "drift": drift, "problems": problems,
            "has_rules": os.path.exists(tpl.rules_path)}


def read_rules(tpl: Template) -> dict:
    if not os.path.exists(tpl.rules_path):
        return {"baseline": [], "blocks": {}, "placeholders": {}}
    with open(tpl.rules_path, encoding="utf-8") as fh:
        return json.load(fh)


def rules_as_deck(spec: dict) -> List[dict]:
    """rules.json -> the ordered list the UI edits.

    The UI's model is one ordered list with a condition per row, and `select()`
    already walks `baseline` dropping blocks whose own `when` is false - so
    this is a faithful reading, not a translation.

    `insert_after` appears only in rules harvested from decks in v2. Honour it
    on read so an existing file opens correctly, and never write it back: an
    author can see a position, so they do not need to describe one.
    """
    blocks = spec.get("blocks") or {}
    order = list(spec.get("baseline") or [])
    pending = [(b, s) for b, s in blocks.items() if b not in set(order)]
    while pending:
        moved = []
        for bid, sub in pending:
            anchor = (sub or {}).get("insert_after")
            if anchor is None:
                order.append(bid)
            elif anchor in order:
                order.insert(order.index(anchor) + 1, bid)
            else:
                continue
            moved.append((bid, sub))
        if not moved:
            order.extend(bid for bid, _s in pending)
            break
        pending = [p for p in pending if p not in moved]

    rows = []
    for bid in order:
        when = (blocks.get(bid) or {}).get("when", "always")
        rows.append({"id": bid,
                     "when": None if when in (None, "always", {}) else when})
    return rows


def deck_as_rules(rows: List[dict], existing: dict) -> dict:
    """The UI's ordered list -> rules.json, keeping everything else intact.

    Bindings, name and library are preserved. They have nothing to do with
    slide selection, and losing them would turn a working template back into
    one client's snapshot.
    """
    spec = dict(existing or {})
    was = (existing or {}).get("blocks") or {}

    blocks = {}
    for r in rows:
        bid = r["id"]
        # Merge into what the block already said, never replace it. The UI
        # edits two things - order and condition - and a rule can carry more
        # than that: `variant` swaps a slide's content without moving it, and
        # a block may map to several slides. Rebuilding the entry from scratch
        # deleted both, silently, on the first save after an author dragged a
        # row. `insert_after` is the one key deliberately dropped: the UI's
        # order is explicit, so describing a position is not just redundant,
        # it is a second source of truth that can disagree.
        sub = dict(was.get(bid) or {})
        sub.pop("insert_after", None)
        sub["slides"] = sub.get("slides") or [bid]
        sub["when"] = r.get("when") or "always"
        blocks[bid] = sub

    spec["baseline"] = [r["id"] for r in rows]
    spec["blocks"] = blocks
    spec.setdefault("placeholders", (existing or {}).get("placeholders", {}))
    return spec


def diff_decks(before: List[dict], after: List[dict]) -> dict:
    """What a save actually changes, so it is a confirmation and not a write."""
    b = {r["id"]: r for r in before}
    a = {r["id"]: r for r in after}
    key = lambda r: json.dumps(r.get("when"), sort_keys=True)
    added = [i for i in a if i not in b]
    removed = [i for i in b if i not in a]
    changed = [i for i in a if i in b and key(a[i]) != key(b[i])]
    kept_before = [i for i in (r["id"] for r in before) if i in a]
    kept_after = [i for i in (r["id"] for r in after) if i in b]
    reordered = kept_before != kept_after
    return {"added": added, "removed": removed, "conditions": changed,
            "reordered": reordered,
            "nothing": not (added or removed or changed or reordered)}


def write_rules(tpl: Template, spec: dict) -> Optional[str]:
    """Back up, then write. Every path that changes rules.json comes through here.

    Save, reset and restore all overwrite the same file, so the backup cannot
    live in one of them - the one that skipped it would be the one that lost
    the work. -> the backup's filename, or None if there was nothing to back up.
    """
    with WRITE_LOCK:
        backup = None
        if os.path.exists(tpl.rules_path):
            backup = "%s.%s.bak" % (tpl.rules_path, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy(tpl.rules_path, backup)
        with open(tpl.rules_path, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    return os.path.basename(backup) if backup else None


def starter_rules(tpl: Template, keep_bindings: bool = True) -> dict:
    """What a freshly imported library gets: every slide, in the deck's order.

    The same thing `import_deck` writes, so "reset" and "just imported" mean
    the same thing rather than two nearly-identical shapes.
    """
    existing = read_rules(tpl) if os.path.exists(tpl.rules_path) else {}
    with tpl.open_library() as lib:
        order = [b.id for b in lib.ordered()]
        found = sorted(lib.all_placeholders())

    if keep_bindings and existing.get("placeholders"):
        # Bindings are about *values*, not selection, and they are the
        # expensive half to recreate. Keep them across a reset unless asked
        # otherwise; `inspect` already reports any that no longer match a
        # slide, so a stale one is visible rather than silent.
        bindings = dict(existing["placeholders"])
        for p in found:
            bindings.setdefault("{{%s}}" % p, {"from": "field", "field": p})
    else:
        bindings = {("{{%s}}" % p): {"from": "field", "field": p} for p in found}

    return {
        "name": existing.get("name") or tpl.name,
        "library": existing.get("library") or os.path.basename(tpl.library_path),
        "_comment": "Reset to the starter: every slide, in the library's own "
                    "order, always included. Narrow it by giving rows a "
                    "condition, or drag them out of the deck.",
        "baseline": order,
        "blocks": {bid: {"slides": [bid], "when": "always"} for bid in order},
        "placeholders": bindings,
    }


def list_backups(tpl: Template) -> List[dict]:
    """Every kept version of this library's rules, newest first."""
    folder = os.path.dirname(tpl.rules_path)
    stem = os.path.basename(tpl.rules_path)
    out = []
    for name in os.listdir(folder):
        if not (name.startswith(stem + ".") and name.endswith(".bak")):
            continue
        path = os.path.join(folder, name)
        stamp = name[len(stem) + 1:-4]
        try:
            with open(path, encoding="utf-8") as fh:
                spec = json.load(fh)
            slides = len(rules_as_deck(spec))
        except Exception:
            slides = None
        out.append({"file": name, "when": stamp, "slides": slides,
                    "bytes": os.path.getsize(path)})
    return sorted(out, key=lambda b: b["when"], reverse=True)


def selection(tpl: Template, answers: dict) -> dict:
    """Which slides these answers produce. Pure, and fast enough to call live."""
    if not os.path.exists(tpl.rules_path):
        raise HTTPException(409, "this library has no rules yet")
    rules = Rules.load(tpl.rules_path)
    chosen, trace = rules.select(answers)
    with tpl.open_library() as lib:
        titles = {b.id: b.title for b in lib.ordered()}
        phs = {b.id: sorted(b.placeholders) for b in lib.ordered()}
    used: set = set()
    for _bid, sid in chosen:
        used.update(phs.get(sid) or [])

    # Actually resolve, rather than checking a binding merely exists. A starter
    # binding points {{ClientName}} at a field called `ClientName`; the payload
    # calls it `FullClientName`, so the binding is present, wrong, and silent -
    # the deck ships with {{ClientName}} printed on the cover and nothing said
    # so. "Bound" is not the question; "produces a value" is.
    values, _unresolved = bindings.resolve(rules.placeholders, answers,
                                           tpl.data_sources())
    flat = flatten(answers)

    # Two different problems, and telling them apart is the whole point.
    # A binding aimed at a field nobody answers is the AUTHOR's mistake and
    # will be wrong for everyone. A field that exists but was left blank is
    # this USER's answer, and is often perfectly deliberate.
    unbound, empty = [], []
    for p in sorted(used):
        if values.get(p) not in (None, "", [], {}):
            continue
        spec = rules.placeholders.get("{{%s}}" % p)
        if not isinstance(spec, dict):
            unbound.append(p)
        elif spec.get("from") == "field" and spec.get("field") not in flat:
            unbound.append(p)
        else:
            empty.append(p)

    return {"slides": [{"block": bid, "slide": sid, "title": titles.get(sid, sid)}
                       for bid, sid in chosen],
            "count": len(chosen), "unbound": unbound, "empty": empty,
            "trace": trace}


async def take_upload(upload: UploadFile, suffix: str, folder: str) -> str:
    """Stream an upload to disk, refusing anything absurd. -> path."""
    path = os.path.join(folder, "%s%s" % (os.urandom(8).hex(), suffix))
    size = 0
    with open(path, "wb") as fh:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                os.remove(path)
                raise HTTPException(413, "file is larger than %d MB"
                                    % (MAX_UPLOAD // (1024 * 1024)))
            fh.write(chunk)
    if size == 0:
        os.remove(path)
        raise HTTPException(400, "the uploaded file was empty")
    return path


# -------------------------------------------------------------------- API
@app.get("/api/libraries", tags=["libraries"])
def get_libraries():
    """Every library. A template is a folder; this lists them."""
    return [describe(t) for t in list_templates(TEMPLATES)]


@app.post("/api/libraries", tags=["libraries"])
async def post_library(
    file: UploadFile = File(..., description="the .pptx to import"),
    name: str = Form(..., description="becomes the folder under templates/"),
    description: str = Form(""),
    overwrite: bool = Form(False),
):
    """Import a .pptx as a library.

    The name is given, never derived from the filename: it becomes the folder
    and the id every rule and binding refers to, so it is not a label that can
    be tidied up later. Importing over an existing name replaces that library
    *including its rules*, which is why `overwrite` has to be asked for.
    """
    if not NAME_RE.fullmatch(name or ""):
        raise HTTPException(400, "name must be lower case letters, digits and "
                                 "underscores - it becomes a folder name")
    tmpdir = tempfile.mkdtemp(prefix="pptgen3_import_")
    try:
        path = await take_upload(file, ".pptx", tmpdir)
        with WRITE_LOCK:
            report = import_deck(path, TEMPLATES, name=name,
                                 description=description, overwrite=overwrite)
    except TemplateError as exc:
        # The engine names the file it was handed, which here is a temp path
        # nobody asked about. Say the name they uploaded instead - an error
        # that talks about our scratch directory reads like a crash.
        raise HTTPException(409, str(exc).replace(path, file.filename or "the file"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"imported": report, "inspect": inspect(find_template(TEMPLATES, name))}


@app.get("/api/libraries/{lib}", tags=["libraries"])
def get_library(tpl: Template = Depends(library)):
    """Ids, placeholders, and anything that will bite later."""
    return inspect(tpl)


@app.get("/api/libraries/{lib}/thumbs", tags=["libraries"])
def get_thumbs(tpl: Template = Depends(library),
               blocks: str = Query("", description="comma-separated block ids")):
    """Slide thumbnails as SVG, rendered in-process.

    No PowerPoint and no LibreOffice: `engine/svg.py` reads the OOXML directly,
    which is what makes a browsable library possible at all.
    """
    want = [b for b in blocks.split(",") if b]
    results = svgmod.render_template(tpl, block_ids=want or None)
    return [{"id": r["id"], "title": r["title"], "svg": r.get("svg"),
             "width": r.get("width"), "height": r.get("height"),
             "placeholders": r.get("placeholders") or [],
             "error": r.get("error")} for r in results]


@app.get("/api/libraries/{lib}/rules", tags=["rules"])
def get_rules(tpl: Template = Depends(library)):
    spec = read_rules(tpl)
    return {"raw": spec, "deck": rules_as_deck(spec),
            "placeholders": spec.get("placeholders") or {}}


@app.put("/api/libraries/{lib}/rules", tags=["rules"])
def put_rules(body: SaveRules, tpl: Template = Depends(library)):
    """Save rules - as an explicit overwrite, never a silent one.

    An author editing a template is usually *updating* rules that already
    exist, which is the case a prototype quietly designs away. So: refuse
    before writing rather than after, back up every write, and report what
    changed instead of reporting success.
    """
    rows = [r.model_dump() for r in body.deck]
    if not rows:
        raise HTTPException(400, "a deck with no slides is not a deck")

    existing = read_rules(tpl)
    spec = deck_as_rules(rows, existing)
    if body.placeholders is not None:
        spec["placeholders"] = body.placeholders

    candidate = Rules(spec, source="rules.json (unsaved)")
    with tpl.open_library() as lib:
        problems = candidate.check_against(lib)
    if problems:
        raise HTTPException(422, "these rules would not build: "
                                 + "; ".join(problems))

    change = diff_decks(rules_as_deck(existing), rows)
    return {"saved": True, "changed": change,
            "backup": write_rules(tpl, spec)}


@app.post("/api/libraries/{lib}/rules/reset", tags=["rules"])
def post_reset(body: ResetRules = ResetRules(),
               tpl: Template = Depends(library)):
    """Start the rules over: every slide, library order, no conditions.

    The state a freshly imported library is in — which is what you want while
    trying a new library, when the rules on screen describe a different deck.

    It backs up first, like every other write, so "reset" is recoverable rather
    than final. Bindings survive by default: they are about values, not
    selection, and they are the expensive half to rebuild.
    """
    before = rules_as_deck(read_rules(tpl))
    spec = starter_rules(tpl, keep_bindings=body.keep_bindings)
    backup = write_rules(tpl, spec)
    after = rules_as_deck(spec)
    return {"reset": True, "backup": backup,
            "deck": after, "placeholders": spec["placeholders"],
            "changed": diff_decks(before, after),
            "kept_bindings": body.keep_bindings}


@app.get("/api/libraries/{lib}/rules/backups", tags=["rules"])
def get_backups(tpl: Template = Depends(library)):
    """Every kept version, newest first. Nothing here is deleted automatically."""
    return list_backups(tpl)


@app.post("/api/libraries/{lib}/rules/restore", tags=["rules"])
def post_restore(body: RestoreRules, tpl: Template = Depends(library)):
    """Put a previous version of the rules back.

    Validated exactly like a save. A backup taken against a *different* library
    can name slides that no longer exist, and restoring it would leave a
    rules.json that cannot build — so it is refused with the reason, and reset
    is the way out.
    """
    if "/" in body.backup or "\\" in body.backup or not body.backup.endswith(".bak"):
        raise HTTPException(400, "not a backup name: %r" % body.backup)
    path = os.path.join(os.path.dirname(tpl.rules_path), body.backup)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such backup: %s" % body.backup)
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)

    with tpl.open_library() as lib:
        problems = Rules(spec, source=body.backup).check_against(lib)
    if problems:
        raise HTTPException(422, "that version does not fit this library: "
                                 + "; ".join(problems)
                                 + " — reset the rules instead")

    before = rules_as_deck(read_rules(tpl))
    after = rules_as_deck(spec)
    return {"restored": body.backup, "backup": write_rules(tpl, spec),
            "deck": after, "placeholders": spec.get("placeholders") or {},
            "changed": diff_decks(before, after)}


@app.post("/api/libraries/{lib}/select", tags=["build"])
def post_select(body: Answers, tpl: Template = Depends(library)):
    """Which slides these answers give - without building anything."""
    return selection(tpl, body.answers)


@app.post("/api/libraries/{lib}/build", tags=["build"])
def post_build(body: Answers, tpl: Template = Depends(library)):
    """Build a deck and hand back a one-shot download link.

    Every build gets its own path. Two builds sharing an output file is the
    first thing that breaks when a second person appears, and it costs nothing
    to avoid now.
    """
    sel = selection(tpl, body.answers)
    token = os.urandom(8).hex()
    build_template(tpl, body.answers, os.path.join(BUILDS, token + ".pptx"))
    return {"token": token, "slides": sel["count"], "unbound": sel["unbound"],
            "empty": sel["empty"],
            "download": "/download/%s" % token,
            "filename": "%s.pptx" % os.path.basename(tpl.folder)}


def build_path(token: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        raise HTTPException(400, "bad build token")
    path = os.path.join(BUILDS, token + ".pptx")
    if not os.path.exists(path):
        raise HTTPException(404, "that build is gone")
    return path


def slide_titles(path: str) -> List[str]:
    """Block titles in file order, for the filmstrip's tooltips."""
    from engine.library import Library
    with Library(path) as lib:
        return [b.title for b in lib.ordered()]


def slides_as_svg(path: str) -> List[dict]:
    from engine.library import Library
    out = []
    with Library(path) as lib:
        for i, block in enumerate(lib.ordered(), 1):
            try:
                r = svgmod.render_slide(lib, block.part)
            except Exception as exc:      # one bad slide must not lose the rest
                r = {"svg": None, "error": "%s: %s" % (type(exc).__name__, exc)}
            out.append({"index": i, "title": block.title, "png": None,
                        "svg": r.get("svg"),
                        "unsupported": r.get("unsupported") or [],
                        "error": r.get("error")})
    return out


@app.get("/api/builds/{token}/slides", tags=["build"])
def build_slides(token: str,
                 engine: str = Query("auto", pattern="^(auto|powerpoint|svg)$")):
    """The built deck, slide by slide — the real thing where we can get it.

    It shows the deck *after* selection and after every placeholder was filled,
    which is the only version anyone actually receives. Downloading and opening
    PowerPoint to find a stray `{{...}}` is a slow way to learn something the
    screen can say.

    Two renderers, and the answer says which one drew it:

      **powerpoint** — the slides exported by PowerPoint itself. Exact, because
      it *is* PowerPoint: real fonts, real wrapping, charts and SmartArt.

      **svg** — `engine/svg.py`, in-process and always available. An honest
      approximation, and on a template that leans on inherited styling it can
      look like a skeleton of the deck. Good enough to answer "which slide is
      this"; not good enough to answer "is this what the client sees".

    `engine=powerpoint` refuses rather than quietly falling back, so a caller
    that needs fidelity can tell the difference.
    """
    path = build_path(token)

    why = None
    if engine in ("auto", "powerpoint"):
        ok, why = rendermod.available()
        if ok:
            try:
                pngs = rendermod.deck_to_images(path, os.path.join(RENDERS, token))
                titles = slide_titles(path)
                return {"engine": "powerpoint", "why": None, "slides": [
                    {"index": i, "title": titles[i - 1] if i <= len(titles) else "",
                     "png": "/api/builds/%s/png/%d" % (token, i),
                     "svg": None, "unsupported": [], "error": None}
                    for i in range(1, len(pngs) + 1)]}
            except rendermod.RenderError as exc:
                why = str(exc)
        if engine == "powerpoint":
            raise HTTPException(503, why or "PowerPoint is not available here")

    return {"engine": "svg", "why": why, "slides": slides_as_svg(path)}


@app.get("/api/builds/{token}/png/{index}", tags=["build"])
def build_png(token: str, index: int):
    """One exported slide image. Written by the call above; never rendered here,
    so a missing file means the export was cleared, not that it failed."""
    build_path(token)
    path = os.path.join(RENDERS, token, "slide-%03d.png" % index)
    if not os.path.exists(path):
        raise HTTPException(404, "that slide has not been rendered")
    return FileResponse(path, media_type="image/png")


@app.get("/download/{token}", tags=["build"])
def download(token: str):
    if not re.fullmatch(r"[0-9a-f]{16}", token or ""):
        raise HTTPException(400, "bad download token")
    path = os.path.join(BUILDS, token + ".pptx")
    if not os.path.exists(path):
        raise HTTPException(404, "that build is gone")
    return FileResponse(
        path, filename="proposal.pptx",
        media_type="application/vnd.openxmlformats-officedocument"
                   ".presentationml.presentation")


def payload_dir(lib: str) -> str:
    """Answer sets live under the library they describe.

    They were in one shared folder, which is wrong the moment a second library
    exists: two templates ask different questions, and mixing them offers an
    author fields from a form their deck has never seen. Nothing warns, because
    a field that exists somewhere looks exactly like a field that exists here.
    """
    d = os.path.join(PAYLOADS, lib)
    os.makedirs(d, exist_ok=True)
    return d


def read_payloads_for(lib: str):
    """-> (payloads, shared) - `shared` if these came from the old flat folder.

    Files uploaded before payloads were scoped sit loose in data/payloads/.
    Orphaning them silently would look like the app had lost them, so they are
    still read when a library has none of its own, and reported as borrowed.
    """
    own = payloadsmod.read_all(payload_dir(lib))
    if own:
        return own, False
    return payloadsmod.read_all(PAYLOADS), True


def fields_for(lib: str) -> dict:
    payloads, shared = read_payloads_for(lib)
    return {"fields": payloadsmod.catalogue(payloads), "shared": bool(shared),
            "payloads": [{"label": label, "answers": answers}
                         for label, answers in payloads]}


@app.get("/api/libraries/{lib}/fields", tags=["answers"])
def get_fields(tpl: Template = Depends(library)):
    """The one list of fields, read from this library's answer sets.

    It fills the build form *and* the condition editor, so a form and a rule
    can never disagree about a field's name or which values it takes. Typing a
    field name by hand is how a rule silently stops matching; the UI never
    offers the chance.
    """
    return fields_for(os.path.basename(tpl.folder))


@app.post("/api/libraries/{lib}/payloads", tags=["answers"])
def post_payload(body: SavePayload, tpl: Template = Depends(library)):
    """Keep an answer set so the catalogue can learn its fields and values.

    Adding is by name: a new name is kept alongside the others, and the same
    name replaces that one file. Everything present is read back into the
    catalogue, so more answer sets mean fewer fields that look like they never
    vary.
    """
    lib = os.path.basename(tpl.folder)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", body.name)[:64] or "payload"
    path = os.path.join(payload_dir(lib), safe + ".json")
    replaced = os.path.exists(path)
    with WRITE_LOCK:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body.answers, fh, indent=2, ensure_ascii=False)
    out = fields_for(lib)
    out["replaced"] = replaced
    out["saved"] = safe
    return out


@app.delete("/api/libraries/{lib}/payloads/{name}", tags=["answers"])
def delete_payload(name: str, tpl: Template = Depends(library)):
    """Drop an answer set.

    A wrong one is not harmless: every field and value in it joins the
    catalogue, so it can put a field in the condition editor that no real
    request contains. Uploading was reversible only by editing the disk.
    """
    lib = os.path.basename(tpl.folder)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64]
    path = os.path.join(payload_dir(lib), safe + ".json")
    if not os.path.isfile(path):
        raise HTTPException(404, "no answer set called %r for %s" % (name, lib))
    with WRITE_LOCK:
        os.remove(path)
    return fields_for(lib)


@app.post("/api/libraries/{lib}/suggest", tags=["rules"])
async def post_suggest(
    decks: List[UploadFile] = File(..., description="the decks these rules "
                                                    "should produce"),
    tpl: Template = Depends(library),
):
    """Read the decks and say what the rules should be. Applies nothing.

    Answer sets come from this library's own, uploaded on the Questions screen,
    and decks pair with them by name - `01_thing.pptx` with `01_thing.json`. A
    deck whose payload is missing can only say which slides it holds, never
    why, and the report says which those were.

    If rules already exist it reports **what changes**, not everything. A second
    batch should surface the two new things rather than restating the forty an
    author has already confirmed.
    """
    from engine import suggest as suggestmod
    from engine import suggest_md

    name = os.path.basename(tpl.folder)
    payloads = dict(read_payloads_for(name)[0])
    tmpdir = tempfile.mkdtemp(prefix="pptgen3_suggest_")
    try:
        paths = []
        for upload in decks:
            path = await take_upload(upload, ".pptx", tmpdir)
            # Keep the uploaded name: it is how a deck finds its answer set.
            named = os.path.join(tmpdir, os.path.basename(upload.filename or "deck.pptx"))
            if named != path:
                os.replace(path, named)
            paths.append(named)

        try:
            report = suggestmod.analyse(tpl.library_path, sorted(paths), payloads,
                                        block_map=tpl.raw_block_map)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

        current = rules_as_deck(read_rules(tpl))
        if current:
            diff = suggestmod.compare(report, current)
            markdown = suggest_md.changes(report, diff)
        else:
            diff = None
            with tpl.open_library() as lib:
                phs = sorted(lib.all_placeholders())
            markdown = suggest_md.full(report, placeholders=phs)
        return {"markdown": markdown, "diff": diff,
                "decks": [d["label"] for d in report["decks"]],
                "unpaired": report["unpaired"],
                "incremental": bool(current)}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/api/compare", tags=["check"])
async def post_compare(
    ours: UploadFile = File(..., description="the deck this tool built"),
    templafy: UploadFile = File(..., description="the deck Templafy generated"),
    lib: str = Form("", description="library id, only to name displaced blocks"),
):
    """v1's deck diff, over HTTP.

    Once rules are authored by hand rather than inferred, this is the only
    objective answer about whether a deck is right. Both files are read here
    and deleted; neither is stored.
    """
    tmpdir = tempfile.mkdtemp(prefix="pptgen3_compare_")
    try:
        theirs_path = await take_upload(templafy, ".pptx", tmpdir)
        ours_path = await take_upload(ours, ".pptx", tmpdir)
        names = {}
        if lib:
            try:
                names = block_names(find_template(TEMPLATES, lib))
            except Exception:        # naming is a courtesy, not the job
                names = {}
        return compare(theirs_path, ours_path, names=names)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ static
# Mounted last so it never shadows /api or /download.
app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
