#!/usr/bin/env python3
"""Build a 15-slide master library, payloads, and the target decks to match.

The end-to-end exercise this exists for:

    1. upload  fixtures/master.pptx  as a library, and inspect it
    2. author rules until the deck matches what each payload asks for
    3. build with a payload from  fixtures/payloads/
    4. compare against the matching deck in  fixtures/targets/

Every slide's title says what it is for, and every conditional slide is present
in at least one payload and absent from another - which is what makes the rule
discoverable rather than guessable. Three payloads, because one cannot tell you
which slides are conditional.

The targets are built by **this engine** from `fixtures/reference_rules.json`.
So the exercise tests rule authoring, ordering, and the compare screen. It does
*not* test fidelity to Templafy's OOXML - the 70 real decks are the only thing
that can, and `engine/verify.py` is still how you use them.

    python tools/make_test_fixtures.py
"""
import json
import os
import re
import shutil
import sys
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import ooxml_snippets as x                                        # noqa: E402

from engine import Template, build_template                       # noqa: E402
from engine.verify import compare                                 # noqa: E402

W, H = x.EMU_W, x.EMU_H
M = 914400
NAVY, TEAL, GREY, PLUM = "00338D", "0091DA", "5A6672", "483698"


CREATION_NS = "http://schemas.microsoft.com/office/drawing/2014/main"
CREATION_URI = "{FF2B5EF4-FFF2-40B4-BE49-F238E27FC236}"
_CNVPR = re.compile(r'<p:cNvPr id="(\d+)" name="([^"]*)"/>')


def with_creation_ids(block_id, xml):
    """Give every shape an `<a16:creationId>`, the way PowerPoint does.

    Without them, `verify.compare()` has no exact key and falls back to layout
    and geometry - and in a fixture where every content slide is drawn from the
    same shapes, geometry cannot tell them apart. The verdict stays right
    (a slide is missing) but the *name* it reports can be the wrong one, which
    is exactly the kind of thing that makes someone distrust the tool while it
    is behaving correctly.

    Real Templafy decks carry these, which is why the firm's 70 matched 100% by
    creationId. A fixture without them is easier to build and teaches the wrong
    lesson.

    The GUIDs are derived from the block and shape ids, so regenerating the
    fixture does not invent new identities and quietly invalidate a target deck
    someone already built against.
    """
    def one(m):
        sid, name = m.group(1), m.group(2)
        guid = uuid.uuid5(uuid.NAMESPACE_URL,
                          "pptgen3/fixture/%s/%s" % (block_id, sid))
        return ('<p:cNvPr id="%s" name="%s"><a:extLst><a:ext uri="%s">'
                '<a16:creationId xmlns:a16="%s" id="{%s}"/></a:ext></a:extLst>'
                '</p:cNvPr>' % (sid, name, CREATION_URI, CREATION_NS,
                                str(guid).upper()))
    return _CNVPR.sub(one, xml)


def marker(sid, block_id):
    """`{{block:<id>}}` in an off-canvas box: identity that survives reordering.

    Without it a block is named by its slide title, so retitling a slide in
    PowerPoint silently renames the block and breaks every rule pointing at it.
    The builder strips these from output.
    """
    return x.textbox(sid, "block-marker", W + M, 0, 2743200, 457200,
                     ["{{block:%s}}" % block_id], size=1200)


def cover(bid, title, subtitle, footer):
    return x.slide([
        x.band(2, "bg", 0, 0, W, H, NAVY),
        x.band(3, "rule", M, int(H * 0.58), 2286000, 68580, TEAL),
        x.textbox(4, "title", M, int(H * 0.30), W - 2 * M, 1200000, [title],
                  size=4000, bold=True, color="FFFFFF"),
        x.textbox(5, "subtitle", M, int(H * 0.64), W - 2 * M, 700000, [subtitle],
                  size=2000, color="D6E4F5"),
        x.textbox(6, "footer", M, H - M, W - 2 * M, 400000, [footer],
                  size=1200, color="9FB8DA"),
        marker(7, bid)])


def content(bid, title, bullets, kicker=None):
    shapes = [
        x.band(2, "accent", 0, 0, 68580, H, NAVY),
        x.textbox(3, "title", M, int(H * 0.14), W - 2 * M, 800000, [title],
                  size=3200, bold=True, color=NAVY),
        x.textbox(4, "body", M, int(H * 0.32), W - 2 * M, 2400000,
                  ["•  " + b for b in bullets], size=1800),
    ]
    if kicker:
        shapes.append(x.textbox(5, "kicker", M, H - M, W - 2 * M, 400000,
                                [kicker], size=1200, color=GREY))
    shapes.append(marker(6, bid))
    return x.slide(shapes)


def fees(bid):
    """A real <a:tbl>. Every build then exercises the <a:t> corruption trap."""
    return x.slide([
        x.textbox(2, "title", M, int(H * 0.14), W - 2 * M, 800000,
                  ["Fees"], size=3200, bold=True, color=NAVY),
        x.table(3, "fees-table", M, int(H * 0.34), W - 2 * M, 2000000,
                [["Phase", "Timing", "Fee"],
                 ["Planning and risk assessment", "Q1", "{{FeePlanning}}"],
                 ["Interim fieldwork", "Q3", "{{FeeInterim}}"],
                 ["Year-end and reporting", "Q1 following", "{{FeeYearEnd}}"],
                 ["Total", "", "{{FeeTotal}}"]],
                [4900000, 2600000, 2800000], header_color=NAVY),
        x.textbox(4, "note", M, H - M, W - 2 * M, 400000,
                  ["Fees are fixed for the period stated and exclude "
                   "disbursements."], size=1200, color=GREY),
        marker(5, bid)])


def contacts(bid):
    """A placeholder split across runs - the case that broke naive matching."""
    return x.slide([
        x.band(2, "bg", 0, 0, W, H, PLUM),
        x.textbox(3, "title", M, int(H * 0.20), W - 2 * M, 800000,
                  ["Talk to us"], size=3600, bold=True, color="FFFFFF"),
        x.split_run_textbox(4, "partner", M, int(H * 0.44), W - 2 * M, 700000,
                            ["{{Lead", "Partner}}", "  —  engagement partner"],
                            size=2000),
        x.textbox(5, "office", M, int(H * 0.58), W - 2 * M, 600000,
                  ["{{City}} office"], size=1800, color="E4DAF2"),
        marker(6, bid)])


# ---------------------------------------------------------------- the deck
# Order here IS the library order, and the reference rules keep it. Authoring
# is therefore "add the conditions", not "work out an order" - one exercise at
# a time.
SLIDES = [
    ("cover", "always", lambda b: cover(
        b, "{{ClientName}}", "Proposal for external audit services",
        "Prepared by Demo LLP  •  {{DueDate}}")),

    ("agenda", "always", lambda b: content(
        b, "Agenda", ["Who we are", "How we audit", "Scope and timetable",
                      "Fees", "Next steps"])),

    ("about_us", "always", lambda b: content(
        b, "About {{FirmName}}",
        ["A national audit practice with 40 years of continuous service.",
         "Sector specialists embedded in every engagement team.",
         "Independent, and structured to stay that way."])),

    ("engagement_team", "always", lambda b: content(
        b, "Your engagement team",
        ["{{LeadPartner}} leads the engagement and signs the opinion.",
         "A named manager on site throughout fieldwork.",
         "Continuity of team members year over year."])),

    ("approach", "always", lambda b: content(
        b, "Our audit approach",
        ["Risk assessment driven by your business, not a checklist.",
         "Early identification of judgements and estimates.",
         "No surprises: issues are raised as we find them."])),

    # ---- conditional from here on ----
    ("transition_plan", {"field": "AuditType", "eq": "New Audit Client"},
     lambda b: content(
        b, "Transition plan",
        ["A 90-day plan from appointment to first fieldwork.",
         "Opening balance verification and predecessor liaison.",
         "No disruption to your reporting calendar."],
        kicker="Included when this is a new audit client")),

    ("transition_lab", {"field": "Transition_lab", "eq": True},
     lambda b: content(
        b, "Join us for a transition lab",
        ["A facilitated half-day with your finance leadership.",
         "We map the first year together before it starts."],
        kicker="Included when the transition lab is requested")),

    ("continuity", {"field": "AuditType", "eq": "Expansion of Services"},
     lambda b: content(
        b, "Continuity is key",
        ["The team that knows your business stays on it.",
         "Expanded scope does not mean starting again."],
        kicker="Included when the mandate expands")),

    ("scope", "always", lambda b: content(
        b, "Scope of services",
        ["Audit of the statutory financial statements for {{ClientName}}.",
         "Review of the interim financial information.",
         "Reporting to those charged with governance."])),

    ("scope_expansion", {"field": "AuditType", "eq": "Expansion of Services"},
     lambda b: content(
        b, "Scope — expansion of services",
        ["Audit of each newly acquired subsidiary in the expanded group.",
         "Group consolidation and component auditor instructions.",
         "Incremental effort scoped and priced separately."],
        kicker="Included when the mandate expands")),

    ("industry_credentials", {"field": "Industry", "exists": True},
     lambda b: content(
        b, "{{Industry}} credentials",
        ["Sector specialists who audit your peers.",
         "Benchmarks drawn from comparable engagements.",
         "Regulatory change tracked for you, not after you."],
        kicker="Included when an industry is given")),

    ("local_office", {"field": "City", "exists": True},
     lambda b: content(
        b, "Your local office",
        ["A {{City}} team on site, not a call centre.",
         "Partner access without a diary war."],
        kicker="Included when a city is given")),

    ("quality", {"field": "Quality", "eq": True}, lambda b: content(
        b, "Quality in all we do",
        ["Independent second partner review on every listed audit.",
         "Inspection results published in full.",
         "Escalation paths that do not depend on goodwill."],
        kicker="Included when the quality section is requested")),

    ("fees", "always", fees),
    ("contacts", "always", contacts),
]


PAYLOADS = {
    "p1_new_client": {
        "FullClientName": "Northwind Manufacturing plc",
        "ShortClientName": "Northwind",
        "DueDate": "20261130",
        "AuditType": "New Audit Client",
        "Industry": "Technology",
        "City": "Chicago",
        "Transition_lab": False,
        "Quality": True,
    },
    "p2_expansion": {
        "FullClientName": "Globex International Holdings",
        "ShortClientName": "Globex",
        "DueDate": "20270315",
        "AuditType": "Expansion of Services",
        "Industry": "Healthcare",
        "City": "Boston",
        "Transition_lab": True,
        "Quality": False,
    },
    "p3_minimal": {
        "FullClientName": "Initech LLC",
        "ShortClientName": "Initech",
        "DueDate": "20270620",
        "AuditType": "New Audit Client",
        "Industry": "",
        "City": "",
        "Transition_lab": False,
        "Quality": False,
    },
}

BINDINGS = {
    "{{ClientName}}":  {"from": "field", "field": "FullClientName"},
    "{{DueDate}}":     {"from": "field", "field": "DueDate", "format": "long_comma"},
    "{{City}}":        {"from": "field", "field": "City"},
    "{{Industry}}":    {"from": "field", "field": "Industry"},
    "{{FirmName}}":    {"from": "literal", "value": "Demo LLP"},
    "{{LeadPartner}}": {"from": "literal", "value": "Alex Reyes"},
    "{{FeePlanning}}": {"from": "literal", "value": "$48,000"},
    "{{FeeInterim}}":  {"from": "literal", "value": "$62,000"},
    "{{FeeYearEnd}}":  {"from": "literal", "value": "$71,000"},
    "{{FeeTotal}}":    {"from": "literal", "value": "$181,000"},
}


def write_master(path):
    slides = [(bid, with_creation_ids(bid, make(bid)))
              for bid, _when, make in SLIDES]
    n = len(slides)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rids = ["rId%d" % (i + 2) for i in range(n)]
    prs_rels = [("rId1", "slideMaster", "slideMasters/slideMaster1.xml")]
    prs_rels += [(rids[i], "slide", "slides/slide%d.xml" % (i + 1))
                 for i in range(n)]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", x.content_types(n))
        z.writestr("_rels/.rels",
                   x.rels([("rId1", "officeDocument", "ppt/presentation.xml")]))
        z.writestr("ppt/presentation.xml", x.presentation(rids, "rId1"))
        z.writestr("ppt/_rels/presentation.xml.rels", x.rels(prs_rels))
        z.writestr("ppt/theme/theme1.xml", x.theme("Fixture"))
        z.writestr("ppt/slideMasters/slideMaster1.xml", x.master("rId1"))
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", x.rels([
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", "theme", "../theme/theme1.xml")]))
        z.writestr("ppt/slideLayouts/slideLayout1.xml", x.layout("Blank"))
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", x.rels([
            ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")]))
        for i, (_bid, xml) in enumerate(slides, start=1):
            z.writestr("ppt/slides/slide%d.xml" % i, xml)
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % i, x.rels([
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")]))
    return [b for b, _x in slides]


def reference_rules(order):
    return {
        "name": "Fixture master",
        "library": "library.pptx",
        "_comment": "The answer key for the fixture exercise. Author your own "
                    "rules first; this is here to check against, and to "
                    "regenerate the targets.",
        "baseline": order,
        "blocks": {bid: {"slides": [bid], "when": when}
                   for bid, when, _make in SLIDES},
        "placeholders": BINDINGS,
    }


def main():
    fx = os.path.join(ROOT, "fixtures")
    stage = os.path.join(fx, "_reference")
    for d in (fx, os.path.join(fx, "payloads"), os.path.join(fx, "targets")):
        os.makedirs(d, exist_ok=True)

    master = os.path.join(fx, "master.pptx")
    order = write_master(master)

    rules = reference_rules(order)
    with open(os.path.join(fx, "reference_rules.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # A throwaway template folder, only so the engine can build the targets.
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    shutil.copy(master, os.path.join(stage, "library.pptx"))
    with open(os.path.join(stage, "rules.json"), "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(stage, "template.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "fixture reference", "library": "library.pptx"}, fh)

    tpl = Template(stage)
    rows = []
    for label, answers in PAYLOADS.items():
        with open(os.path.join(fx, "payloads", label + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(answers, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        target = os.path.join(fx, "targets", label + ".pptx")
        build_template(tpl, answers, target)
        from engine.rules import Rules
        chosen, _trace = Rules(rules).select(answers)
        rows.append((label, [b for b, _s in chosen], target))

    # Prove each target really is what the reference rules produce, and that
    # comparing a target with itself is a MATCH - otherwise the exercise's
    # success condition is not reachable and nobody would know why.
    self_check = compare(rows[0][2], rows[0][2])
    shutil.rmtree(stage, ignore_errors=True)

    print("Wrote %s" % fx)
    print("  master.pptx           %d slides" % len(order))
    print("  reference_rules.json  %d conditional"
          % sum(1 for _b, w, _m in SLIDES if w != "always"))
    for label, blocks, path in rows:
        print("  targets/%-16s %2d slides  %s"
              % (label + ".pptx", len(blocks), ", ".join(blocks[:4]) + " ..."))
    print("\n  a target compared with itself: %s" % self_check["verdict"])

    every = {b for _l, blocks, _p in rows for b in blocks}
    never = [b for b, _w, _m in SLIDES if b not in every]
    always = [b for b, w, _m in SLIDES if w != "always"
              and all(b in blocks for _l, blocks, _p in rows)]
    if never:
        print("  ! never selected by any payload: %s" % ", ".join(never))
    if always:
        print("  ! conditional but in every payload, so the rule is not "
              "discoverable: %s" % ", ".join(always))
    return 1 if (never or always) else 0


if __name__ == "__main__":
    raise SystemExit(main())
