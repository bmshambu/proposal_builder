#!/usr/bin/env python3
"""Walk the fixture exercise end to end, through the API the browser uses.

    python tools/check_fixtures.py

Upload master.pptx -> set the rules -> build each payload -> compare with the
matching target. Every step is the same HTTP call the UI makes.

The point is to know the exercise is *passable* before handing it to anyone.
A fixture set that cannot reach MATCH wastes an afternoon and looks like a bug
in the tool.
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                          # noqa: E402

import app as appmod                                               # noqa: E402

client = TestClient(appmod.app)
FX = os.path.join(ROOT, "fixtures")
LIB = "fixture_master"

PASS, FAIL = [], []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print("  %-4s %-40s %s" % ("ok" if ok else "FAIL", label, detail))


def main():
    if not os.path.exists(os.path.join(FX, "master.pptx")):
        print("no fixtures yet - run tools/make_test_fixtures.py first")
        return 1

    folder = os.path.join(ROOT, "templates", LIB)
    shutil.rmtree(folder, ignore_errors=True)

    # 1 -------------------------------------------------- upload and inspect
    with open(os.path.join(FX, "master.pptx"), "rb") as fh:
        r = client.post("/api/libraries",
                        files={"file": ("master.pptx", fh.read())},
                        data={"name": LIB, "description": "fixture exercise"})
    check("upload master.pptx", r.status_code == 200, r.text[:60])
    if r.status_code != 200:
        return 1
    insp = r.json()["inspect"]
    check("inspect finds every slide", len(insp["blocks"]) == 15,
          "%d blocks, %d placeholders" % (len(insp["blocks"]),
                                          len(insp["placeholders"])))
    check("ids come from markers, not titles",
          insp["by_source"].get("marker") == 15,
          "%s - retitling a slide cannot break a rule" % insp["by_source"])
    check("split-run placeholder is found",
          "LeadPartner" in insp["placeholders"],
          "{{Lead}}{{Partner}} across runs is still one placeholder")

    # 2 ------------------------------------------------------- set the rules
    ref = json.load(open(os.path.join(FX, "reference_rules.json"),
                         encoding="utf-8"))
    deck = [{"id": bid,
             "when": None if (ref["blocks"][bid]["when"] == "always")
                     else ref["blocks"][bid]["when"]}
            for bid in ref["baseline"]]
    r = client.put("/api/libraries/%s/rules" % LIB,
                   json={"deck": deck, "placeholders": ref["placeholders"]})
    check("save the rules", r.status_code == 200,
          "%d rows, %d conditional"
          % (len(deck), sum(1 for d in deck if d["when"])))

    # 3 ------------------------------------ build each payload and compare
    for label in sorted(os.listdir(os.path.join(FX, "payloads"))):
        if not label.endswith(".json"):
            continue
        name = label[:-5]
        answers = json.load(open(os.path.join(FX, "payloads", label),
                                 encoding="utf-8"))
        sel = client.post("/api/libraries/%s/select" % LIB,
                          json={"answers": answers}).json()
        r = client.post("/api/libraries/%s/build" % LIB, json={"answers": answers})
        built = r.json()
        check("build %s" % name, r.status_code == 200 and built["slides"],
              "%d slides, %d unbound placeholder(s)"
              % (built["slides"], len(built["unbound"])))
        check("%s leaves no placeholder unbound" % name, not built["unbound"],
              ", ".join(built["unbound"]) or "every value resolved")

        blob = client.get(built["download"]).content
        target = os.path.join(FX, "targets", name + ".pptx")
        with open(target, "rb") as fh:
            r = client.post("/api/compare",
                            files={"ours": ("ours.pptx", blob),
                                   "templafy": ("target.pptx", fh.read())},
                            data={"lib": LIB})
        res = r.json()
        check("%s matches its target" % name, res["verdict"] == "MATCH",
              "%s - %d vs %d slides, %d matched (%s)"
              % (res["verdict"], res["our_slides"], res["templafy_slides"],
                 res["matched"],
                 ", ".join("%s %d" % kv for kv in sorted(res["by_method"].items()))))

    # 4 ------------------------------------- a wrong rule must be detectable
    broken = [d for d in deck if d["id"] != "quality"]
    client.put("/api/libraries/%s/rules" % LIB,
               json={"deck": broken, "placeholders": ref["placeholders"]})
    answers = json.load(open(os.path.join(FX, "payloads", "p1_new_client.json"),
                             encoding="utf-8"))
    built = client.post("/api/libraries/%s/build" % LIB,
                        json={"answers": answers}).json()
    blob = client.get(built["download"]).content
    with open(os.path.join(FX, "targets", "p1_new_client.pptx"), "rb") as fh:
        res = client.post("/api/compare",
                          files={"ours": ("ours.pptx", blob),
                                 "templafy": ("target.pptx", fh.read())},
                          data={"lib": LIB}).json()
    check("a missing slide is reported, and named",
          res["verdict"] == "SELECTION DIFFERS" and res["missing"],
          "%s - missing %s" % (res["verdict"],
                               res["missing"][0]["preview"][:34] if res["missing"] else "-"))

    shutil.rmtree(folder, ignore_errors=True)
    print("\n%d passed, %d failed %s"
          % (len(PASS), len(FAIL), ", ".join(FAIL) if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
