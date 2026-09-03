"""
FastAPI app for the proposal builder POC.

Stages:  Construct  ->  Map  ->  Reconstruct
  Construct   : build a payload + call your Templafy generation script.
  Map         : learn selection + token rules from (payload -> deck) pairs.
  Reconstruct : rebuild a deck for a new payload from the learned rules only.

Run:  uvicorn app.main:app --reload    (from the repo root)
Then open http://127.0.0.1:8000
"""
import os
import pathlib

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, schema, services, constraints

app = FastAPI(title="Proposal Builder POC")
BASE = pathlib.Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


def _ctx(request, **extra):
    ctx = {
        "request": request,
        "master_present": config.MASTER_PPTX.exists(),
        "script_present": config.GENERATE_SCRIPT.exists(),
        "n_payloads": len(services.list_payloads()),
        "n_decks": len(services.list_decks()),
        "n_outputs": len(services.list_outputs()),
        "spec_summary": services.spec_summary(),
    }
    ctx.update(extra)
    return ctx


# ------------------------------------------------------------------ dashboard
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


# ------------------------------------------------------------------ Construct
@app.get("/construct", response_class=HTMLResponse)
def construct_get(request: Request, msg: str = ""):
    last_log = config.LAST_LOG.read_text(encoding="utf-8") if config.LAST_LOG.exists() else ""
    return templates.TemplateResponse(request, "construct.html", _ctx(
        request, schema=schema, opts=constraints.effective_options(),
        payloads=[os.path.basename(p) for p in services.list_payloads()],
        decks=[os.path.basename(d) for d in services.list_decks()],
        default_email=config.default_email(), last_log=last_log, msg=msg))


@app.post("/construct/sync-constraints")
def construct_sync_constraints():
    res = constraints.sync()
    return RedirectResponse("/construct?msg=" + res["msg"], status_code=303)


async def _collect_form(request):
    form = await request.form()
    return {k: v for k, v in form.items()}


@app.post("/construct/run")
async def construct_run(request: Request):
    form = await _collect_form(request)
    name = form.get("name") or (form.get("short_client_name") or "payload")
    payload = schema.build_payload(form)
    path = services.save_payload(name, payload)
    msg = "Saved payload %s." % os.path.basename(path)
    if form.get("generate") == "1":
        res = services.construct(path, email=form.get("email"))
        msg += (" Generated deck: %s" % os.path.basename(res["deck"])) if res["ok"] \
            else " Generation FAILED - see server log."
    return RedirectResponse("/construct?msg=" + msg, status_code=303)


@app.post("/construct/generate-one")
def construct_generate_one(payload: str = Form(...), email: str = Form(None)):
    path = str(config.PAYLOADS_DIR / payload)
    res = services.construct(path, email=email)
    msg = ("Generated %s" % os.path.basename(res["deck"])) if res["ok"] else "Generation FAILED"
    return RedirectResponse("/construct?msg=" + msg, status_code=303)


@app.post("/construct/generate-all")
async def construct_generate_all(request: Request):
    form = await _collect_form(request)
    only_missing = form.get("only_missing") == "1"
    res = services.construct_all(email=form.get("email"), only_missing=only_missing)
    msg = ("Batch done — generated %d, skipped %d, failed %d."
           % (len(res["generated"]), len(res["skipped"]), len(res["failed"])))
    if res["failed"]:
        msg += " Failed: " + ", ".join(res["failed"])
    return RedirectResponse("/construct?msg=" + msg, status_code=303)


@app.get("/api/subsectors")
def subsectors(sector: str):
    opts = constraints.effective_options()
    return JSONResponse(opts["subsectors_by_sector"].get(sector, []))


# ------------------------------------------------------------------ Map
@app.get("/map", response_class=HTMLResponse)
def map_get(request: Request):
    report = config.MAPPING_REPORT.read_text(encoding="utf-8") if config.MAPPING_REPORT.exists() else ""
    return templates.TemplateResponse(request, "map.html", _ctx(request, report=report))


@app.post("/map/run")
def map_run(request: Request):
    services.run_map()
    return RedirectResponse("/map", status_code=303)


# ------------------------------------------------------------------ Reconstruct
@app.get("/reconstruct", response_class=HTMLResponse)
def reconstruct_get(request: Request, msg: str = ""):
    return templates.TemplateResponse(request, "reconstruct.html", _ctx(
        request, schema=schema, opts=constraints.effective_options(),
        payloads=[os.path.basename(p) for p in services.list_payloads()],
        outputs=[os.path.basename(o) for o in services.list_outputs()],
        msg=msg))


@app.post("/reconstruct/run")
async def reconstruct_run(request: Request):
    form = await _collect_form(request)
    # either reconstruct an existing payload, or build a new one from the form
    if form.get("existing"):
        path = str(config.PAYLOADS_DIR / form["existing"])
    else:
        name = form.get("name") or (form.get("short_client_name") or "payload")
        payload = schema.build_payload(form)
        path = services.save_payload(name, payload)
    res = services.reconstruct(path)
    msg = ("Rebuilt %s" % os.path.basename(res["out"])) if res["ok"] else ("FAILED: " + res["log"][:200])
    return RedirectResponse("/reconstruct?msg=" + msg, status_code=303)


# ------------------------------------------------------------------ Diff
@app.get("/diff", response_class=HTMLResponse)
def diff_get(request: Request):
    files = services.diff_pick_files()
    return templates.TemplateResponse(request, "diff.html", _ctx(
        request, pairs=services.suggest_diff_pairs(),
        decks=files, outputs=files, result=None))


@app.post("/diff/run", response_class=HTMLResponse)
def diff_run(request: Request, original: str = Form(...), rebuilt: str = Form(...)):
    res = services.run_diff(original, rebuilt)
    files = services.diff_pick_files()
    return templates.TemplateResponse(request, "diff.html", _ctx(
        request, pairs=services.suggest_diff_pairs(),
        decks=files, outputs=files,
        result=res.get("result"), err=None if res["ok"] else res["log"],
        sel_original=original, sel_rebuilt=rebuilt))


# ------------------------------------------------------------------ download
@app.get("/download")
def download(path: str):
    p = (config.DATA / path).resolve()
    if not str(p).startswith(str(config.DATA.resolve())) or not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(p), filename=p.name)
