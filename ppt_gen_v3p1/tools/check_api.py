"""Exercise every endpoint against the real engine. -> non-zero on failure.

    python tools/check_api.py

Uses FastAPI's TestClient, so it needs no running server and no port. It is
not a mock: every call goes through the same routes, the same dependencies and
the same engine as a browser request, against the synthetic demo template.

It covers the refusals as well as the successes. A save that writes broken
rules over working ones is the worst outcome this app has available, so
"refuses to" is as much a feature as "does".
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                          # noqa: E402

import app as appmod                                               # noqa: E402
from engine import Template, build_template                        # noqa: E402

client = TestClient(appmod.app)

PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print("  %-4s %-36s %s" % ("ok" if ok else "FAIL", label, detail))


def _no_network(*_args, **_kwargs):
    """Anyone running these on a machine with a working `.env` would otherwise
    be one import away from a check suite uploading a deck to somebody's real
    SharePoint to find out whether a test passes.

    This used to guard only the section that talks to Graph. Then import
    learned to make a library PDF, and ordinary import checks started reaching
    the tenant - so the guard belongs around the whole run, and the credentials
    go out of the environment with it.
    """
    raise AssertionError("a check tried to reach the real Microsoft Graph")


def main():
    from engine import graph as graphmod

    graph_env = {k: os.environ.pop(k, None) for k in graphmod.ENV}
    wire = graphmod._wire
    graphmod._wire = _no_network
    try:
        return _run()
    finally:
        graphmod._wire = wire
        graphmod.TRANSPORT = None
        graphmod.forget_apps()
        for key, was in graph_env.items():
            if was is not None:
                os.environ[key] = was


def _run():
    demo = os.path.join(ROOT, "templates", "demo")
    if not os.path.isdir(demo):
        print("no demo template at %s" % demo)
        return 1

    # ---------------------------------------------------------- libraries
    r = client.get("/api/libraries")
    libs = r.json()
    check("GET /api/libraries", r.status_code == 200 and len(libs) >= 1,
          "%d library" % len(libs))

    r = client.get("/api/libraries/demo")
    insp = r.json()
    check("GET /api/libraries/demo", r.status_code == 200 and insp["blocks"],
          "%d blocks, %d placeholders, %d unbound"
          % (len(insp["blocks"]), len(insp["placeholders"]),
             len(insp["unbound"])))

    r = client.get("/api/libraries/demo/thumbs", params={"blocks": "cover,fees"})
    thumbs = r.json()
    check("GET /thumbs", r.status_code == 200 and all(t["svg"] for t in thumbs),
          "%d svg, first %d chars" % (len(thumbs), len(thumbs[0]["svg"] or "")))
    check("thumbnails are real SVG",
          all("<svg" in (t["svg"] or "") for t in thumbs),
          "rendered in-process, no PowerPoint")

    # -------------------------------------------------------------- rules
    r = client.get("/api/libraries/demo/rules")
    rules = r.json()
    conds = [x for x in rules["deck"] if x["when"]]
    check("GET /rules", r.status_code == 200 and len(rules["deck"]) >= 7,
          "%d rows, %d bindings" % (len(rules["deck"]), len(rules["placeholders"])))
    check("insert_after read as a flat order", bool(conds),
          json.dumps(conds[0]) if conds else "no conditions found")

    # ------------------------------------------------- reset and restore
    # Run before anything that depends on the demo's authored rules, and put
    # them back at the end - a check that leaves the fixture altered is a
    # check you can only run once.
    # Keep the bytes, not just the meaning. Restore rewrites the file through
    # json.dump, which preserves what the rules *say* while reformatting a
    # hand-authored file - enough to leave the repo dirty after every run. A
    # check that alters a committed fixture is one people stop running.
    rules_path = os.path.join(demo, "rules.json")
    with open(rules_path, "rb") as fh:
        original_bytes = fh.read()
    original = json.loads(original_bytes.decode("utf-8"))

    r = client.post("/api/libraries/demo/rules/reset", json={})
    reset = r.json()
    check("POST /rules/reset", r.status_code == 200,
          "%d rows, backup %s" % (len(reset["deck"]), reset["backup"]))
    check("reset clears every condition",
          all(row["when"] is None for row in reset["deck"]),
          "every slide always included, library order")
    check("reset keeps the bindings by default",
          len(reset["placeholders"]) == len(original.get("placeholders", {})),
          "%d binding(s) survived" % len(reset["placeholders"]))
    check("reset says what it changed", not reset["changed"]["nothing"],
          "conditions: %s" % (reset["changed"]["conditions"] or "-"))

    backups = client.get("/api/libraries/demo/rules/backups").json()
    check("GET /rules/backups", backups and backups[0]["file"].endswith(".bak"),
          "%d version(s), newest %s" % (len(backups), backups[0]["when"]))

    r = client.post("/api/libraries/demo/rules/restore",
                    json={"backup": backups[0]["file"]})
    restored = r.json()
    check("POST /rules/restore", r.status_code == 200,
          "%d rows back" % len(restored["deck"]))
    check("restore actually brings the conditions back",
          any(row["when"] for row in restored["deck"]),
          "reset is recoverable, not final")

    r = client.post("/api/libraries/demo/rules/restore",
                    json={"backup": "../../../etc/passwd"})
    check("restore refuses a path", r.status_code == 400,
          r.json().get("detail", "")[:44])
    r = client.post("/api/libraries/demo/rules/restore",
                    json={"backup": "rules.json.19990101-000000.bak"})
    check("restore refuses a missing backup", r.status_code == 404,
          r.json().get("detail", "")[:44])

    now = json.load(open(rules_path, encoding="utf-8"))
    check("the demo template is back as it was",
          now.get("baseline") == original.get("baseline")
          and now.get("blocks") == original.get("blocks"),
          "restore round-tripped the fixture")

    # A save rewrites every block entry. Anything the UI does not edit - a
    # `variant`, a block mapping to several slides - has to survive it, or an
    # author loses a rule by dragging a row and pressing save.
    r = client.get("/api/libraries/demo/rules")
    deck = r.json()["deck"]
    r = client.put("/api/libraries/demo/rules", json={"deck": deck})
    check("save keeps what the UI does not edit", r.status_code == 200,
          "saved %d rows unchanged" % len(deck))
    after = json.load(open(rules_path, encoding="utf-8"))
    for bid, sub in (original.get("blocks") or {}).items():
        if "variant" in sub:
            check("variant on %r survives a save" % bid,
                  "variant" in (after.get("blocks") or {}).get(bid, {}),
                  "content-swap rules are not selection, and are not the UI's "
                  "to delete")
        if len(sub.get("slides") or []) > 1:
            check("multi-slide block %r keeps its slides" % bid,
                  (after["blocks"][bid].get("slides") == sub["slides"]))
    check("save drops insert_after",
          not any("insert_after" in s for s in (after.get("blocks") or {}).values()),
          "the UI's order is explicit - a second source of truth can disagree")

    # put the fixture back byte for byte, so this suite can be run twice
    with open(rules_path, "wb") as fh:
        fh.write(original_bytes)
    for name in os.listdir(demo):
        if name.startswith("rules.json.") and name.endswith(".bak"):
            os.remove(os.path.join(demo, name))
    with open(rules_path, "rb") as fh:
        check("the fixture is byte-identical afterwards",
              fh.read() == original_bytes,
              "running the checks leaves the repo clean")

    # ---------------------------------------------------------- selection
    exp = json.load(open(os.path.join(demo, "answers.expansion.json"),
                         encoding="utf-8"))
    base = json.load(open(os.path.join(demo, "answers.baseline.json"),
                          encoding="utf-8"))
    a = client.post("/api/libraries/demo/select", json={"answers": exp}).json()
    b = client.post("/api/libraries/demo/select", json={"answers": base}).json()
    check("POST /select", a["count"] > 0,
          "%d slides: %s" % (a["count"], ", ".join(s["block"] for s in a["slides"])))
    check("selection follows the answers", a["count"] != b["count"],
          "expansion %d vs baseline %d" % (a["count"], b["count"]))

    # -------------------------------------------------------------- build
    r = client.post("/api/libraries/demo/build", json={"answers": exp})
    built = r.json()
    check("POST /build", r.status_code == 200 and built["slides"] == a["count"],
          "%d slides, token %s" % (built["slides"], built["token"][:8]))
    d = client.get(built["download"])
    check("GET /download", d.status_code == 200 and d.content[:2] == b"PK",
          "%d bytes, a real zip" % len(d.content))
    # The viewer renders the BUILT file, not the library - so it shows what
    # selection produced and what every placeholder became. A preview of the
    # library would show {{ClientName}} and be reassuring about the wrong thing.
    # Two renderers. `svg` is always there; `powerpoint` is the real thing and
    # is what the viewer asks for, so both paths are checked - including that
    # the answer says which one drew it, because a preview that quietly
    # degrades to an approximation is a preview nobody can use as evidence.
    body = client.get("/api/builds/%s/slides?engine=svg" % built["token"]).json()
    slides = body["slides"]
    check("GET /builds/{token}/slides?engine=svg",
          body["engine"] == "svg" and len(slides) == built["slides"],
          "%d slide(s), all rendered: %s"
          % (len(slides), all(s["svg"] for s in slides)))
    check("the preview shows filled values, not placeholders",
          not any("{{" in (s["svg"] or "") for s in slides),
          "first slide reads %r" % (slides[0]["title"][:38] if slides else ""))

    from engine import render as rendermod
    # The library backend is the one that deploys: previews are pages copied out
    # of a PDF of the library, so nothing converts anything at render time. It
    # wins over the converters whenever the library has a PDF.
    auto = client.get("/api/builds/%s/slides" % built["token"]).json()
    check("the viewer is told which renderer drew the deck",
          auto["engine"] == "library",
          "%s%s" % (auto["engine"], "" if not auto.get("why") else
                    " - %s" % auto["why"]))
    check("every slide came back as an image",
          len(auto["slides"]) == built["slides"]
          and all(s["png"] and not s["svg"] for s in auto["slides"]),
          "%d image(s)" % len(auto["slides"]))
    img = client.get(auto["slides"][0]["png"])
    check("GET /builds/{token}/png/{n}",
          img.status_code == 200 and img.content[1:4] == b"PNG",
          "%d bytes, a real png" % len(img.content))
    check("a slide nobody exported is 404",
          client.get("/api/builds/%s/png/999" % built["token"]).status_code == 404)

    # The mapping is the whole risk. If page N stops being slide N the preview
    # is confidently wrong - it shows a real slide from the real library, just
    # not the one in the deck. So check it against the selection, and then check
    # the bytes actually served are the bytes of that page.
    with open(os.path.join(appmod.BUILDS, built["token"] + ".json")) as fh:
        note = json.load(fh)
    tpl_demo = appmod.find_template(appmod.TEMPLATES, "demo")
    demo_pptx_before = open(os.path.join(tpl_demo.folder, "library.pptx"),
                            "rb").read()
    picked = client.post("/api/libraries/demo/select",
                         json={"answers": exp}).json()["slides"]
    with tpl_demo.open_library() as lib:
        want = [lib.block(row["slide"]).index for row in picked]
    check("the build records which library pages it is made of",
          note["pages"] == want and note["lib"] == "demo",
          "pages %s" % ", ".join(str(p + 1) for p in note["pages"]))

    served = client.get(auto["slides"][1]["png"]).content
    one_page = os.path.join(tempfile.mkdtemp(prefix="pptgen3_page_"), "x")
    _e, direct = rendermod.preview_from_library(
        os.path.join(tpl_demo.folder, "library.pdf"), [want[1]], one_page)
    check("preview slide 2 really is the library page the rules chose",
          served == open(direct[0], "rb").read(),
          "library page %d, byte for byte" % (want[1] + 1))

    # ------------------------------------------------- slide text, for editing
    #
    # The read side of marking placeholders in the tool. Two properties matter
    # and nothing else here does:
    #
    #   * every box lands on the slide - a box in the wrong place means an
    #     author marks the wrong words with complete confidence, which is the
    #     same failure class as a preview showing the wrong page;
    #   * a run's span cuts the paragraph text exactly - that span is the
    #     address a write will resolve, so if it drifts the placeholder lands
    #     in the wrong run.
    r = client.get("/api/libraries/demo/slides/1/text")
    tm = r.json()
    check("GET /libraries/demo/slides/1/text",
          r.status_code == 200 and tm.get("shapes"),
          "%d shape(s), slide is %d x %d EMU"
          % (len(tm.get("shapes") or []), tm.get("width", 0), tm.get("height", 0)))

    astray = [s["name"] for s in tm["shapes"]
              if s["box"][0] < 0 or s["box"][1] < 0
              or s["box"][0] + s["box"][2] > tm["width"] + 1
              or s["box"][1] + s["box"][3] > tm["height"] + 1]
    check("every shape box lands on the slide", not astray,
          "%d box(es) checked" % len(tm["shapes"]) if not astray
          else "off the slide: " + ", ".join(astray))

    def spans_hold(slide_map):
        """Runs in order, inside the text, not overlapping, and covering
        everything except the line breaks `<a:br>` puts there."""
        for shape in slide_map["shapes"]:
            for para in shape["paragraphs"]:
                text, at = para["text"], 0
                covered = [False] * len(text)
                for run in para["runs"]:
                    if not (at <= run["start"] <= run["end"] <= len(text)):
                        return "%s p%d: run span %d-%d does not fit" % (
                            shape["name"], para["index"], run["start"], run["end"])
                    for i in range(run["start"], run["end"]):
                        covered[i] = True
                    at = run["end"]
                loose = set(text[i] for i in range(len(text)) if not covered[i])
                if loose - set("\n"):
                    return "%s p%d: %r is in no run" % (
                        shape["name"], para["index"], "".join(sorted(loose))[:12])
        return None

    broke = None
    for n in range(1, len(client.get("/api/libraries/demo").json()["blocks"]) + 1):
        broke = spans_hold(client.get("/api/libraries/demo/slides/%d/text" % n).json())
        if broke:
            break
    check("every run's span cuts its paragraph exactly", broke is None,
          broke or "all slides, all paragraphs")

    # Placeholders already in the library must come back whole, or the editor
    # would offer to mark text that is already a placeholder.
    found = set()
    for shape in tm["shapes"]:
        for para in shape["paragraphs"]:
            found.update(para["placeholders"])
    check("placeholders already on the slide are reported",
          "ClientName" in found, ", ".join(sorted(found)) or "none")

    # The picture the editor draws its boxes over. It has to be the real page:
    # the boxes come from the OOXML, so they land correctly on a true render
    # and nowhere useful on a redrawn approximation.
    img = client.get("/api/libraries/demo/slides/1/png")
    check("GET /libraries/demo/slides/1/png",
          img.status_code == 200 and img.content[1:4] == b"PNG",
          "%d bytes, a real png" % len(img.content))
    check("a slide image that does not exist is 404",
          client.get("/api/libraries/demo/slides/99/png").status_code == 404)

    check("a slide that does not exist is 404",
          client.get("/api/libraries/demo/slides/99/text").status_code == 404)
    check("slide text does not change the library",
          open(os.path.join(tpl_demo.folder, "library.pptx"), "rb").read()
          == demo_pptx_before,
          "library.pptx is byte-identical after reading it")

    # Coverage is the property that decides whether this feature is usable at
    # all: an author who can mark six placeholders out of ten is an author who
    # still has to open PowerPoint. Measured against the library, not asserted.
    def reachable(lib_name, slides):
        seen, cells, gaps = set(), 0, []
        for n in range(1, slides + 1):
            page = client.get("/api/libraries/%s/slides/%d/text" % (lib_name, n)).json()
            gaps += page.get("unreachable") or []
            for shape in page["shapes"]:
                if shape.get("kind") == "cell":
                    cells += 1
                for para in shape["paragraphs"]:
                    seen.update(para["placeholders"])
        return seen, cells, gaps

    demo_blocks = client.get("/api/libraries/demo").json()
    want = {p.strip("{}").strip() for p in demo_blocks["placeholders"]}
    got, _cells, gaps = reachable("demo", len(demo_blocks["blocks"]))
    check("every placeholder in the library is reachable", want <= got,
          "%d of %d" % (len(want & got), len(want)) +
          ("" if want <= got else " - missing " + ", ".join(sorted(want - got))))

    # The fixture library is the realistic one: 15 slides, and a fee table that
    # holds four of its ten placeholders. Before tables were readable this
    # check would have failed at 6 of 10.
    with open(os.path.join(ROOT, "fixtures", "master.pptx"), "rb") as fh:
        r = client.post("/api/libraries",
                        files={"file": ("master.pptx", fh.read())},
                        data={"name": "fx_tables"})
    if r.status_code == 200:
        fx = client.get("/api/libraries/fx_tables").json()
        fx_want = {p.strip("{}").strip() for p in fx["placeholders"]}
        fx_got, fx_cells, fx_gaps = reachable("fx_tables", len(fx["blocks"]))
        check("text inside a table can be marked", fx_cells > 0,
              "%d cell(s) across %d slide(s)" % (fx_cells, len(fx["blocks"])))
        check("a fee table does not hide its placeholders", fx_want <= fx_got,
              "%d of %d reachable" % (len(fx_want & fx_got), len(fx_want)) +
              ("" if fx_want <= fx_got else " - missing " +
               ", ".join(sorted(fx_want - fx_got))))
        check("nothing on a real library is unreachable", not fx_gaps,
              "; ".join(g["why"] for g in fx_gaps[:2]) if fx_gaps else "no gaps")

        astray, broke = [], None
        for n in range(1, len(fx["blocks"]) + 1):
            page = client.get("/api/libraries/fx_tables/slides/%d/text" % n).json()
            astray += [s["name"] for s in page["shapes"]
                       if s["box"][0] < 0 or s["box"][1] < 0
                       or s["box"][0] + s["box"][2] > page["width"] + 1
                       or s["box"][1] + s["box"][3] > page["height"] + 1]
            broke = broke or spans_hold(page)
        check("every box on a real library lands on the slide", not astray,
              "%d slide(s) checked" % len(fx["blocks"]) if not astray
              else "off the slide: " + ", ".join(sorted(set(astray))[:3]))
        check("run spans hold across a real library", broke is None,
              broke or "15 slides, tables included")
    else:
        check("text inside a table can be marked", False,
              "could not import the fixture library: %d" % r.status_code)
    shutil.rmtree(os.path.join(appmod.TEMPLATES, "fx_tables"), ignore_errors=True)
    shutil.rmtree(os.path.join(ROOT, "data", "payloads", "fx_tables"),
                  ignore_errors=True)

    # ------------------------------------------- marking, and saving it away
    #
    # The promise this feature makes is not that it writes a placeholder
    # correctly - it is that the library an author started from is still there
    # if it does not. So the byte-for-byte check on the original matters more
    # than the rest, and it is made after a save that worked *and* after every
    # save that was refused.
    #
    # Graph is taken out of the picture for these: a check suite that reaches a
    # live tenant to make a preview PDF is one nobody can run on a train.
    mk_env = {k: os.environ.pop(k, None) for k in
              ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET",
               "GRAPH_DRIVE_ID")}
    mk_made = []
    try:
        mk_page = client.get("/api/libraries/demo/slides/1/text").json()
        mk_para = [q for sh in mk_page["shapes"] for q in sh["paragraphs"]
                   if q["text"].startswith("Proposal for")][0]
        mk_at = mk_para["text"].index("external audit")
        mk_one = {"part": mk_page["part"], "at": mk_para["at"],
                  "expect": mk_para["text"], "start": mk_at,
                  "end": mk_at + len("external audit"), "name": "ServiceLine",
                  "binding": {"from": "literal", "value": "external audit"}}

        r = client.post("/api/libraries/demo/mark",
                        json={"name": "demo_marked", "description": "a check",
                              "marks": [mk_one]})
        mk_out = r.json()
        if r.status_code == 200:
            mk_made.append("demo_marked")
        check("POST /libraries/demo/mark saves a new library",
              r.status_code == 200 and mk_out.get("library") == "demo_marked",
              str(mk_out.get("detail") or mk_out.get("library"))[:60])
        check("the library it was marked from is untouched",
              open(os.path.join(tpl_demo.folder, "library.pptx"), "rb").read()
              == demo_pptx_before, "byte for byte")

        mk_new = client.get("/api/libraries/demo_marked").json()
        check("the placeholder is in the new library",
              "ServiceLine" in (mk_new.get("placeholders") or []),
              ", ".join(sorted(mk_new.get("placeholders") or []))[:54])
        check("and every slide came with it",
              len(mk_new.get("blocks") or []) == len(demo_blocks["blocks"]),
              "%d slide(s)" % len(mk_new.get("blocks") or []))

        mk_saved = client.get("/api/libraries/demo_marked/rules").json()
        mk_from = client.get("/api/libraries/demo/rules").json()
        check("the rules travelled with it",
              [row["id"] for row in mk_saved["deck"]]
              == [row["id"] for row in mk_from["deck"]],
              "%d row(s), in the same order" % len(mk_saved["deck"]))
        check("the binding was written with the placeholder",
              (mk_saved["placeholders"].get("{{ServiceLine}}") or {}).get("from")
              == "literal",
              str(mk_saved["placeholders"].get("{{ServiceLine}}"))[:48])

        # A library that cannot build a deck was written badly, whatever its
        # placeholders say.
        mk_deck = client.post("/api/libraries/demo_marked/build",
                              json={"answers": exp})
        check("a deck still builds from the new library",
              mk_deck.status_code == 200 and mk_deck.json()["slides"] > 0,
              "%s slide(s)" % (mk_deck.json().get("slides")
                               if mk_deck.status_code == 200
                               else mk_deck.text[:36]))

        # ---- the refusals, each of which must leave nothing behind
        r = client.post("/api/libraries/demo/mark",
                        json={"name": "demo_stale",
                              "marks": [dict(mk_one,
                                             expect="never said this")]})
        check("text that changed since it was read is refused",
              r.status_code == 409, (r.json().get("detail") or "")[:54])
        check("and a refused save leaves no library behind",
              not os.path.isdir(os.path.join(appmod.TEMPLATES, "demo_stale")))

        r = client.post("/api/libraries/demo/mark",
                        json={"name": "demo_marked", "marks": [mk_one]})
        check("a name already in use is refused", r.status_code == 400,
              (r.json().get("detail") or "")[:50])
        check("marking nothing is refused",
              client.post("/api/libraries/demo/mark",
                          json={"name": "demo_empty", "marks": []}
                          ).status_code == 400)
        check("a name that is not a library id is refused",
              client.post("/api/libraries/demo/mark",
                          json={"name": "Demo Marked", "marks": [mk_one]}
                          ).status_code == 400)
        check("the original is still untouched after every refusal",
              open(os.path.join(tpl_demo.folder, "library.pptx"), "rb").read()
              == demo_pptx_before, "byte for byte")
    finally:
        for gone in mk_made:
            shutil.rmtree(os.path.join(appmod.TEMPLATES, gone),
                          ignore_errors=True)
            shutil.rmtree(os.path.join(ROOT, "data", "payloads", gone),
                          ignore_errors=True)
        for key, was in mk_env.items():
            if was is not None:
                os.environ[key] = was

    rs = client.get("/api/renderers").json()
    check("GET /renderers says what this machine can do",
          {e["engine"] for e in rs["engines"]}
          == {"library", "libreoffice", "powerpoint", "graph", "svg"},
          ", ".join("%s %s" % (e["engine"], "ok" if e["available"] else "no")
                    for e in rs["engines"]))
    for backend in ("libreoffice", "powerpoint"):
        ok, missing = rendermod.probe(backend)
        r = client.get("/api/builds/%s/slides?engine=%s"
                       % (built["token"], backend))
        if ok:
            check("engine=%s renders" % backend,
                  r.status_code == 200 and r.json()["engine"] == backend,
                  "%d slide(s)" % len(r.json()["slides"]))
        else:
            # Refusing beats pretending: a caller that needs fidelity must be
            # able to tell that it did not get it.
            check("engine=%s refuses rather than falling back" % backend,
                  r.status_code == 503, missing)

    # ------------------------------------------------------------- Graph
    #
    # The backend under evaluation, and the only one that leaves this machine.
    # What these prove is not that it can draw a slide - any renderer does
    # that. It is that a client's deck never stays behind in SharePoint, that
    # nothing which is a credential reaches the screen, and that asking for
    # graph gets graph or gets a refusal, never something else wearing its
    # name.
    #
    # There is no tenant here. `tools/fake_graph.py` stands in for the wire;
    # msal, the retries, the >4 MB switch, the 302 and the delete in the
    # `finally` are all the real code running for real. Everything below is
    # therefore proven against a fake, and `docs/graph-findings.md` says so.
    sys.path.insert(0, HERE)
    from engine import graph as graphmod
    from fake_graph import FakeGraph, TOKEN as FAKE_TOKEN, no_sleeping

    def graph_pdf(pages):
        """What Office Online would hand back: one page per slide of the built
        deck. Cut from the demo library PDF so the bytes are a real PDF."""
        import pypdfium2 as pdfium
        out = pdfium.PdfDocument.new()
        out.import_pages(pdfium.PdfDocument(
            os.path.join(tpl_demo.folder, "library.pdf")),
            list(range(pages)))
        buf = io.BytesIO()
        out.save(buf)
        return buf.getvalue()

    def fresh_build():
        """A build of its own. Renders are cached per token, so a scenario that
        reuses one would be answered from the last scenario's PNGs."""
        return client.post("/api/libraries/demo/build",
                           json={"answers": exp}).json()["token"]

    graph_was = {k: os.environ.get(k) for k in graphmod.ENV}
    try:
        for name in graphmod.ENV:
            os.environ.pop(name, None)
        ok, why = rendermod.probe("graph")
        check("graph probes unavailable when it is not configured",
              not ok and "GRAPH_TENANT_ID" in (why or ""),
              (why or "")[:70])
        r = client.get("/api/builds/%s/slides?engine=graph" % built["token"])
        check("engine=graph is 503 when unconfigured, not a fallback",
              r.status_code == 503, (r.json().get("detail") or "")[:60])

        os.environ.update({"GRAPH_TENANT_ID": "fake-tenant",
                           "GRAPH_CLIENT_ID": "fake-client",
                           "GRAPH_CLIENT_SECRET": "fake-secret",
                           "GRAPH_DRIVE_ID": "fake-drive"})
        ok, why = rendermod.probe("graph")
        check("graph probes usable once configured", ok, why or "")

        deck_pdf = graph_pdf(built["slides"])

        # ---- it renders, and the values are the built deck's own
        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf)
        graphmod.TRANSPORT = fake
        token = fresh_build()
        r = client.get("/api/builds/%s/slides?engine=graph" % token)
        body = r.json()
        check("engine=graph renders the built deck",
              r.status_code == 200 and body.get("engine") == "graph"
              and len(body.get("slides") or []) == built["slides"],
              "%s, %d slide(s)" % (body.get("engine"),
                                   len(body.get("slides") or [])))
        img = client.get(body["slides"][0]["png"])
        check("and serves real images",
              img.status_code == 200 and img.content[1:4] == b"PNG",
              "%d bytes" % len(img.content))
        check("the uploaded copy is deleted after a success", fake.deleted,
              " ".join(m for m, _u in fake.calls()))
        # The bug this check exists because of: every one of these passed
        # while twenty-two real decks sat in a real recycle bin. "Deleted" in
        # SharePoint means "moved somewhere still readable".
        check("and purged from the recycle bin, not just moved there",
              fake.deleted and not fake.bin,
              "bin holds %d item(s)" % len(fake.bin))
        check("the bearer token never reaches the pre-authed URL",
              not fake.auth_leaked_to("download.aspx")
              and not fake.auth_leaked_to("uploadSession"),
              "%d request(s) inspected" % len(fake.requests))

        # ---- and the delete is not conditional on any of that working
        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf, fail="convert")
        graphmod.TRANSPORT = fake
        r = client.get("/api/builds/%s/slides?engine=graph" % fresh_build())
        detail = r.json().get("detail") or ""
        check("a conversion that fails mid-render still deletes the copy",
              r.status_code == 503 and fake.deleted,
              "%d, %s" % (r.status_code, " ".join(m for m, _u in fake.calls())))
        check("nothing that is a credential reaches the screen",
              not any(s in detail for s in (FAKE_TOKEN, "fake-secret",
                                            "tempauth")),
              detail[:60])

        # ---- a copy that cannot be removed is an error, not a shrug
        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf, fail="delete")
        graphmod.TRANSPORT = fake
        r = client.get("/api/builds/%s/slides?engine=graph" % fresh_build())
        check("a copy left behind in SharePoint is reported as a failure",
              r.status_code == 503
              and "could NOT be deleted" in (r.json().get("detail") or ""),
              (r.json().get("detail") or "")[:60])

        # ---- a deck stuck in the recycle bin is as bad as one in the folder
        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf, fail="purge")
        graphmod.TRANSPORT = fake
        r = client.get("/api/builds/%s/slides?engine=graph" % fresh_build())
        detail = r.json().get("detail") or ""
        check("a deck left in the recycle bin is reported as a failure",
              r.status_code == 503 and "RECYCLE BIN" in detail,
              detail[:58])

        # ---- no network: degrade, never 500
        graphmod.forget_apps()
        graphmod.TRANSPORT = FakeGraph(offline=True)
        token = fresh_build()
        r = client.get("/api/builds/%s/slides?engine=graph" % token)
        check("engine=graph refuses with a reason when the network is down",
              r.status_code == 503
              and "could not reach" in (r.json().get("detail") or ""),
              (r.json().get("detail") or "")[:55])
        r = client.get("/api/builds/%s/slides" % token)
        check("and auto still previews the deck anyway",
              r.status_code == 200 and r.json()["engine"] != "graph",
              "auto used %s" % r.json().get("engine"))

        # ---- the point of the whole exercise: a .pptx on its own
        #
        # An author uploads one file. Making the library PDF used to mean
        # PowerPoint on somebody's desk, which is why import refused to
        # generate one; Graph needs nothing installed, so that reasoning does
        # not reach it.
        graphmod.forget_apps()
        fake = FakeGraph(pdf=graph_pdf(len(demo_blocks["blocks"])))
        graphmod.TRANSPORT = fake
        with open(tpl_demo.library_path, "rb") as fh:
            r = client.post("/api/libraries",
                            files={"file": ("library.pptx", fh.read())},
                            data={"name": "pptx_only"})
        preview = ((r.json().get("imported") or {}).get("preview") or {})
        check("a .pptx on its own gets its PDF made for it",
              r.status_code == 200 and preview.get("pdf"),
              "%s page(s)" % preview.get("pages") if preview.get("pdf")
              else str(preview.get("why"))[:56])
        check("and the deck it uploaded is still deleted afterwards",
              fake.deleted and not fake.bin, "folder and bin both empty")
        shutil.rmtree(os.path.join(appmod.TEMPLATES, "pptx_only"),
                      ignore_errors=True)

        # Office Online and PowerPoint need not agree about hidden slides, and
        # a PDF with the wrong number of pages is worse than none: every
        # preview after the first missing slide would show a different one.
        graphmod.forget_apps()
        graphmod.TRANSPORT = FakeGraph(pdf=graph_pdf(1))
        with open(tpl_demo.library_path, "rb") as fh:
            r = client.post("/api/libraries",
                            files={"file": ("library.pptx", fh.read())},
                            data={"name": "pptx_short"})
        preview = ((r.json().get("imported") or {}).get("preview") or {})
        check("a generated PDF with the wrong page count is not kept",
              r.status_code == 200 and not preview.get("pdf")
              and "one for one" in (preview.get("why") or ""),
              (preview.get("why") or "")[:54])
        check("and the library still imported without it",
              os.path.isdir(os.path.join(appmod.TEMPLATES, "pptx_short"))
              and not os.path.exists(os.path.join(
                  appmod.TEMPLATES, "pptx_short", "library.pdf")),
              "imported, no PDF, previews fall back")
        shutil.rmtree(os.path.join(appmod.TEMPLATES, "pptx_short"),
                      ignore_errors=True)

        # ---- the parts an API call cannot reach on its own
        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf)
        graphmod.TRANSPORT = fake
        big = os.path.join(tempfile.mkdtemp(prefix="pptgen3_big_"), "big.pptx")
        with open(big, "wb") as fh:
            fh.write(os.urandom(graphmod.SIMPLE_MAX + 1024 * 1024))
        graphmod.from_env().to_pdf(big)
        check("a deck over 4 MB uploads in a session, not one PUT",
              fake.used_session and fake.deleted and not fake.bin,
              "%d chunk(s), %d bytes" % (len(fake.chunks),
                                         fake.body_bytes("PUT")))

        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf, throttle=2, retry_after=3)
        graphmod.TRANSPORT = fake
        undo = no_sleeping(fake)
        try:
            graphmod.from_env().to_pdf(tpl_demo.library_path)
        finally:
            undo()
        check("a throttled tenant is retried, waiting what it asked for",
              fake.slept == [3, 3] and fake.deleted, "waited %s" % fake.slept)

        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf, throttle=1, retry_after=600)
        graphmod.TRANSPORT = fake
        undo = no_sleeping(fake)
        try:
            graphmod.from_env().to_pdf(tpl_demo.library_path)
            gave_up = ""
        except graphmod.GraphError as exc:
            gave_up = str(exc)
        finally:
            undo()
        # Holding a page load open for ten minutes is worse than admitting the
        # preview is not coming and letting the caller fall back.
        check("a Retry-After longer than a page load gives up instead",
              "throttling" in gave_up and not fake.slept, gave_up[:58])

        graphmod.forget_apps()
        fake = FakeGraph(pdf=deck_pdf)
        graphmod.TRANSPORT = fake
        graphmod.from_env().to_pdf(tpl_demo.library_path)
        first = len(fake.calls(contains="login.microsoftonline.com"))
        graphmod.from_env().to_pdf(tpl_demo.library_path)
        second = len(fake.calls(contains="login.microsoftonline.com")) - first
        check("a second render costs no trip to Entra at all",
              first >= 2 and second == 0,
              "%d call(s) cold, %d warm" % (first, second))
    finally:
        graphmod.TRANSPORT = None
        graphmod.forget_apps()
        for key, value in graph_was.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("an unknown renderer is refused",
          client.get("/api/builds/%s/slides?engine=magic"
                     % built["token"]).status_code == 422)
    check("a bad build token is refused",
          client.get("/api/builds/zz/slides").status_code == 400)
    check("a missing build is 404",
          client.get("/api/builds/%s/slides" % ("0" * 16)).status_code == 404)

    again = client.post("/api/libraries/demo/build", json={"answers": exp}).json()
    check("each build gets its own path", again["token"] != built["token"],
          "two builds cannot overwrite each other")

    # ------------------------------------------------------------ compare
    tmp = tempfile.mkdtemp()
    tpl = Template(demo)
    p1, p2 = os.path.join(tmp, "a.pptx"), os.path.join(tmp, "b.pptx")
    build_template(tpl, exp, p1)
    build_template(tpl, base, p2)

    def cmp_call(ours, theirs):
        with open(ours, "rb") as f1, open(theirs, "rb") as f2:
            return client.post("/api/compare",
                               files={"ours": ("ours.pptx", f1.read()),
                                      "templafy": ("t.pptx", f2.read())},
                               data={"lib": "demo"}).json()

    same = cmp_call(p1, p1)
    diff = cmp_call(p2, p1)
    check("POST /compare identical", same["verdict"] == "MATCH", same["verdict"])
    check("POST /compare different", not diff["exact"],
          "%s - %d vs %d slides" % (diff["verdict"], diff["our_slides"],
                                    diff["templafy_slides"]))

    # ---------------------------------------------- importing the library PDF
    #
    # The PDF is uploaded, never generated here: converting needs PowerPoint,
    # and the point of the whole design is that the deployed app has none.
    demo_pptx = open(tpl.library_path, "rb").read()
    demo_pdf = open(os.path.join(tpl.folder, "library.pdf"), "rb").read()

    def import_with(pdf_bytes, name="demo_pdf"):
        files = {"file": ("library.pptx", demo_pptx)}
        if pdf_bytes is not None:
            files["pdf"] = ("library.pdf", pdf_bytes)
        return client.post("/api/libraries", files=files,
                           data={"name": name, "overwrite": "true"})

    r = import_with(demo_pdf)
    body = r.json().get("imported", {}) if r.status_code == 200 else {}
    check("import keeps a PDF that matches the deck",
          r.status_code == 200 and (body.get("preview") or {}).get("pdf"),
          "%d page(s), one per slide" % (body.get("preview") or {}).get("pages", 0))

    # A PDF with the wrong number of pages is worse than no PDF: page N would be
    # a different slide and every preview after it would be confidently wrong.
    short = rendermod.page_count(os.path.join(tpl.folder, "library.pdf"))
    one = io.BytesIO()
    import pypdfium2 as _pdfium
    _doc = _pdfium.PdfDocument.new()
    _doc.import_pages(_pdfium.PdfDocument(os.path.join(tpl.folder, "library.pdf")),
                      pages=[0])
    _doc.save(one)
    r = import_with(one.getvalue(), name="demo_badpdf")
    prev = (r.json().get("imported", {}).get("preview") or {})
    check("a PDF with the wrong page count is refused",
          r.status_code == 200 and not prev.get("pdf")
          and "1 page" in (prev.get("why") or ""),
          (prev.get("why") or "")[:64])
    check("and the mismatched PDF is not kept",
          not os.path.exists(os.path.join(appmod.TEMPLATES, "demo_badpdf",
                                          "library.pdf")),
          "%d-page PDF against %d slides" % (1, short))

    # No PDF is allowed - the library still imports, previews just fall back.
    r = import_with(None, name="demo_nopdf")
    prev = (r.json().get("imported", {}).get("preview") or {})
    check("a library without a PDF still imports, and says what is missing",
          r.status_code == 200 and not prev.get("pdf")
          and "could not be made here" in (prev.get("why") or ""),
          (prev.get("why") or "")[:58])
    nb = client.post("/api/libraries/demo_nopdf/build", json={"answers": exp})
    if nb.status_code == 200:
        r = client.get("/api/builds/%s/slides?engine=library" % nb.json()["token"])
        check("engine=library refuses when the library has no PDF",
              r.status_code == 503 and "no PDF" in r.text, "503, and says why")
        fell = client.get("/api/builds/%s/slides" % nb.json()["token"]).json()
        check("and auto falls back rather than failing",
              fell["engine"] != "library" and len(fell["slides"]) > 0,
              "fell back to %s" % fell["engine"])
    for gone in ("demo_pdf", "demo_badpdf", "demo_nopdf"):
        shutil.rmtree(os.path.join(appmod.TEMPLATES, gone), ignore_errors=True)

    # ------------------------------------------------------------ refusals
    r = client.post("/api/libraries", files={"file": ("x.pptx", b"PK\x03\x04")},
                    data={"name": "Bad Name"})
    check("import refuses a bad name", r.status_code == 400,
          r.json().get("detail", "")[:50])

    r = client.post("/api/libraries", files={"file": ("x.pptx", b"not a zip")},
                    data={"name": "junk"})
    check("import refuses a non-pptx", r.status_code == 409,
          r.json().get("detail", "")[:50])

    r = client.put("/api/libraries/demo/rules",
                   json={"deck": [{"id": "ghost_slide"}]})
    check("save refuses rules that cannot build", r.status_code == 422,
          r.json().get("detail", "")[:58])

    r = client.put("/api/libraries/demo/rules", json={"deck": []})
    check("save refuses an empty deck", r.status_code == 400,
          r.json().get("detail", "")[:50])

    check("rules.json survived the refusals",
          json.load(open(os.path.join(demo, "rules.json"), encoding="utf-8")
                    ).get("baseline"),
          "a rejected save must not damage a working file")

    check("unknown library is 404",
          client.get("/api/libraries/nope").status_code == 404)
    check("bad download token is 400",
          client.get("/download/zzz").status_code == 400)
    check("missing build is 404",
          client.get("/download/" + "0" * 16).status_code == 404)

    # -------------------------------------------------------------- fields
    # Answer sets belong to a library, not to the app. Two templates ask
    # different questions, and one shared folder offered an author fields from
    # a form their deck has never seen - with nothing to show it was wrong,
    # because a field that exists somewhere looks like a field that exists here.
    r = client.get("/api/libraries/demo/fields")
    check("GET /libraries/demo/fields", r.status_code == 200,
          "%d field(s) from %d answer set(s)"
          % (len(r.json()["fields"]), len(r.json()["payloads"])))

    r = client.post("/api/libraries/demo/payloads",
                    json={"name": "probe", "answers": {"OnlyHere": "yes"}})
    check("POST a payload keeps it", r.status_code == 200
          and "OnlyHere" in r.json()["fields"],
          "added %s, %d field(s)" % (r.json().get("saved"),
                                     len(r.json()["fields"])))
    again = client.post("/api/libraries/demo/payloads",
                        json={"name": "probe", "answers": {"OnlyHere": "no"}})
    check("the same name replaces, and says so", again.json().get("replaced"),
          "one file, not two")
    check("a second name is added alongside",
          client.post("/api/libraries/demo/payloads",
                      json={"name": "probe2", "answers": {"OnlyHere": "maybe"}}
                      ).json()["payloads"].__len__() == 2,
          "adding is by name")

    # The isolation itself, which is the reason for the move. A field only one
    # library's answer sets mention must not appear in another's - offering it
    # would let an author write a condition on a question their deck is never
    # asked, and nothing on screen would look wrong.
    import shutil as _sh
    second = os.path.join(ROOT, "templates", "demo_two")
    _sh.rmtree(second, ignore_errors=True)
    with open(os.path.join(demo, "library.pptx"), "rb") as fh:
        client.post("/api/libraries", files={"file": ("l.pptx", fh.read())},
                    data={"name": "demo_two"})
    client.post("/api/libraries/demo_two/payloads",
                json={"name": "other", "answers": {"SomewhereElse": "x"}})
    mine = client.get("/api/libraries/demo/fields").json()["fields"]
    theirs = client.get("/api/libraries/demo_two/fields").json()["fields"]
    check("one library cannot see another's fields",
          "SomewhereElse" in theirs and "SomewhereElse" not in mine,
          "demo_two has it, demo does not")
    check("and not the other way either",
          "OnlyHere" in mine and "OnlyHere" not in theirs)
    _sh.rmtree(second, ignore_errors=True)
    _sh.rmtree(os.path.join(ROOT, "data", "payloads", "demo_two"),
               ignore_errors=True)

    r = client.delete("/api/libraries/demo/payloads/probe")
    check("DELETE drops one", r.status_code == 200
          and len(r.json()["payloads"]) == 1,
          "a wrong answer set can be taken back out")
    client.delete("/api/libraries/demo/payloads/probe2")
    # A later batch of answer sets can bring a field the earlier ones never had.
    # Absence is an answer: the field separates the sets that carry it from the
    # ones that do not, so it is usable and `is answered` is the rule. Counting
    # only the sets that carry it marked such a field "never varies" and sent
    # the author away from a rule that works.
    for i in (1, 2):
        client.post("/api/libraries/demo/payloads",
                    json={"name": "shared%d" % i,
                          "answers": {"Common": "same", "Region": "EMEA"}})
    client.post("/api/libraries/demo/payloads",
                json={"name": "extra",
                      "answers": {"Common": "same", "Region": "EMEA",
                                  "LateArrival": "yes"}})
    f = client.get("/api/libraries/demo/fields").json()["fields"]
    late = f.get("LateArrival") or {}
    check("a field only some answer sets carry is usable",
          late.get("varies") is True and late.get("missing") == 2
          and late.get("seen") == 1,
          "in %s of %s" % (late.get("seen"), late.get("of")))
    check("a field every set answers the same way is still not usable",
          (f.get("Common") or {}).get("varies") is False,
          "identical everywhere, so it explains nothing")
    for name in ("shared1", "shared2", "extra"):
        client.delete("/api/libraries/demo/payloads/%s" % name)

    check("DELETE of a missing one is 404",
          client.delete("/api/libraries/demo/payloads/nope").status_code == 404)

    print("\n%d passed, %d failed %s"
          % (len(PASS), len(FAIL), ", ".join(FAIL) if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
