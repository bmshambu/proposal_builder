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


def main():
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

    rs = client.get("/api/renderers").json()
    check("GET /renderers says what this machine can do",
          {e["engine"] for e in rs["engines"]}
          == {"library", "libreoffice", "powerpoint", "svg"},
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
          and "make_library_pdf" in (prev.get("why") or ""),
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
    check("DELETE of a missing one is 404",
          client.delete("/api/libraries/demo/payloads/nope").status_code == 404)

    print("\n%d passed, %d failed %s"
          % (len(PASS), len(FAIL), ", ".join(FAIL) if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
