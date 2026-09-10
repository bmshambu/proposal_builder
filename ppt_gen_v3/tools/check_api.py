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
    original = json.load(open(os.path.join(demo, "rules.json"), encoding="utf-8"))

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

    now = json.load(open(os.path.join(demo, "rules.json"), encoding="utf-8"))
    check("the demo template is back as it was",
          now.get("baseline") == original.get("baseline")
          and now.get("blocks") == original.get("blocks"),
          "restore round-tripped the fixture")

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
    r = client.get("/api/fields")
    check("GET /api/fields", r.status_code == 200,
          "%d field(s) from %d payload(s)"
          % (len(r.json()["fields"]), len(r.json()["payloads"])))

    print("\n%d passed, %d failed %s"
          % (len(PASS), len(FAIL), ", ".join(FAIL) if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
