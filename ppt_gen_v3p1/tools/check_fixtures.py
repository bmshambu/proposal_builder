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

    # A placeholder whose binding names a field that does not exist is bound,
    # wrong, and silent: import_deck guesses {{ClientName}} -> field
    # `ClientName`, while the payload calls it `FullClientName`. The deck ships
    # with {{ClientName}} on the cover and nothing warns. "Bound" is not the
    # question; "produces a value" is.
    ref0 = json.load(open(os.path.join(FX, "reference_rules.json"),
                          encoding="utf-8"))
    deck0 = [{"id": b, "when": None if ref0["blocks"][b]["when"] == "always"
              else ref0["blocks"][b]["when"]} for b in ref0["baseline"]]
    client.put("/api/libraries/%s/rules" % LIB, json={"deck": deck0})
    a0 = json.load(open(os.path.join(FX, "payloads", "p1_new_client.json"),
                        encoding="utf-8"))
    starter = client.post("/api/libraries/%s/select" % LIB,
                          json={"answers": a0}).json()
    check("a binding to a missing field is reported", starter["unbound"],
          "%d would print as literal text: %s"
          % (len(starter["unbound"]), ", ".join(starter["unbound"][:3])))

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
        check("%s has no broken binding" % name, not built["unbound"],
              ", ".join(built["unbound"]) or "every binding names a real field")
        if built.get("empty"):
            check("%s reports its blank answers" % name, True,
                  "%s left blank by this payload — reported, not called an error"
                  % ", ".join(built["empty"]))

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

    # 3b ------------------------------------------- suggesting, and re-suggesting
    # The answer sets have to be in the library for the endpoint to use them:
    # a deck whose payload is missing can say which slides it holds, never why.
    for label in sorted(os.listdir(os.path.join(FX, "payloads"))):
        if label.endswith(".json"):
            client.post("/api/libraries/%s/payloads" % LIB,
                        json={"name": label[:-5],
                              "answers": json.load(open(
                                  os.path.join(FX, "payloads", label),
                                  encoding="utf-8"))})

    def suggest(paths):
        files = [("decks", (os.path.basename(p), open(p, "rb").read()))
                 for p in paths]
        return client.post("/api/libraries/%s/suggest" % LIB, files=files).json()

    every = [os.path.join(FX, "targets", f)
             for f in sorted(os.listdir(os.path.join(FX, "targets")))
             if f.endswith(".pptx")]

    r = suggest(every)
    check("suggest against correct rules finds nothing to do",
          r["diff"] and r["diff"]["nothing_to_do"],
          "%d rule(s) already match and are not repeated" % len(r["diff"]["settled"]))
    check("and says so rather than restating them",
          "Nothing to change" in r["markdown"]
          and "quality" not in r["markdown"].split("## Nothing")[1],
          "settled rules are counted, not listed")

    # Drop one condition and it must come back as NEW, not buried in a re-listing
    thinned = [dict(d, when=None) if d["id"] == "quality" else d for d in deck]
    client.put("/api/libraries/%s/rules" % LIB,
               json={"deck": thinned, "placeholders": ref["placeholders"]})
    r = suggest(every)
    new = [n["block"] for n in r["diff"]["new"]]
    check("a missing condition is reported as new", new == ["quality"],
          "new: %s — and %d settled rule(s) stay quiet"
          % (new, len(r["diff"]["settled"])))

    # Point a rule at the wrong field and it must be reported as a disagreement
    wrong = [dict(d, when={"field": "City", "eq": "Boston"})
             if d["id"] == "quality" else d for d in deck]
    client.put("/api/libraries/%s/rules" % LIB,
               json={"deck": wrong, "placeholders": ref["placeholders"]})
    r = suggest(every)
    changed = [c["block"] for c in r["diff"]["changed"]]
    check("a wrong condition is reported as changed", changed == ["quality"],
          "changed: %s" % changed)
    check("and it shows both sides", "you have:" in r["markdown"]
          and "these decks say:" in r["markdown"],
          "yours and the evidence, side by side")

    client.put("/api/libraries/%s/rules" % LIB,
               json={"deck": deck, "placeholders": ref["placeholders"]})

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
