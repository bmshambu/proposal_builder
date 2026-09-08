#!/usr/bin/env python3
"""Generate `templates/demo/library.pptx` — the non-confidential stand-in for a
designer-authored library deck.

Real templates are made in PowerPoint (decision D1); this exists only so the
engine has a committed fixture to build and regression-test against. It is
deliberately awkward in the two places v1 got burned:

  * a real `<a:tbl>` slide (the `<a:t>` corruption trap), and
  * a placeholder split across several `<a:r>` runs (the risk in v2-plan §8).

Usage:  python tools/make_demo_library.py [out.pptx]
"""
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ooxml_snippets as x                                    # noqa: E402

W, H = x.EMU_W, x.EMU_H
M = 914400                       # 1" margin
NAVY, TEAL, GREY = "00558C", "00A3A1", "5A6672"


MARKERS = True          # set False to emit an "untouched firm template" fixture


def marker(sid, block_id):
    """The block marker: an off-canvas text box holding `{{block:<id>}}`.

    Decision D-marker (v2-plan §7.1) resolved in favour of a marker box: it is
    explicit, survives slide reordering, and the builder strips it from output.
    Off-canvas so it never shows in the library deck either.
    """
    if not MARKERS:
        return ""                    # the unmarked fixture: ids come from titles
    return x.textbox(sid, "block-marker", W + M, 0, 2743200, 457200,
                     ["{{block:%s}}" % block_id], size=1200)


def title_slide(block_id, title, subtitle, footer):
    return x.slide([
        x.band(2, "bg", 0, 0, W, H, NAVY),
        x.band(3, "rule", M, int(H * 0.56), 2286000, 68580, TEAL),
        x.textbox(4, "title", M, int(H * 0.28), W - 2 * M, 1600200,
                  [title], size=4400, bold=True, color="FFFFFF"),
        x.textbox(5, "subtitle", M, int(H * 0.62), W - 2 * M, 800100,
                  [subtitle], size=2000, color="EEF2F6"),
        x.textbox(6, "footer", M, H - M, W - 2 * M, 457200,
                  [footer], size=1400, color="9FB6CA"),
        marker(7, block_id),
    ])


def content_slide(block_id, title, bullets, kicker=None):
    shapes = [
        x.band(2, "accent", 0, 0, 137160, H, NAVY),
        x.textbox(3, "title", M, int(M * 0.6), W - 2 * M, 900000,
                  [title], size=3200, bold=True, color=NAVY),
        x.band(4, "rule", M, int(M * 0.6) + 950000, 1143000, 45720, TEAL),
        x.textbox(5, "body", M, int(M * 0.6) + 1200000, W - 2 * M, H - 2600000,
                  ["•  " + b for b in bullets], size=1800),
    ]
    nid = 6
    if kicker:
        shapes.append(x.textbox(nid, "kicker", M, H - int(M * 1.3), W - 2 * M,
                                457200, [kicker], size=1400, color=GREY))
        nid += 1
    shapes.append(marker(nid, block_id))
    return x.slide(shapes)


def fees_slide(block_id):
    """The table block — every build must survive this slide untouched."""
    rows = [["Phase", "Timing", "Fee"],
            ["Planning & risk assessment", "Q1", "{{FeePlanning}}"],
            ["Interim fieldwork", "Q2–Q3", "{{FeeInterim}}"],
            ["Year-end audit & reporting", "Q4", "{{FeeYearEnd}}"],
            ["Total", "", "{{FeeTotal}}"]]
    col = [(W - 2 * M) // 2, (W - 2 * M) // 4, (W - 2 * M) // 4]
    return x.slide([
        x.band(2, "accent", 0, 0, 137160, H, NAVY),
        x.textbox(3, "title", M, int(M * 0.6), W - 2 * M, 900000,
                  ["Proposed fees"], size=3200, bold=True, color=NAVY),
        x.table(4, "fee-table", M, int(M * 0.6) + 1100000, W - 2 * M, 2600000,
                rows, col, header_color=NAVY),
        x.textbox(5, "note", M, H - int(M * 1.2), W - 2 * M, 457200,
                  ["Fees are exclusive of VAT and out-of-pocket expenses."],
                  size=1200, color=GREY),
        marker(6, block_id),
    ])


def contacts_slide(block_id):
    """Contacts — the placeholder here is split across runs on purpose."""
    return x.slide([
        x.band(2, "bg", 0, 0, W, H, "EEF2F6"),
        x.textbox(3, "title", M, int(M * 0.8), W - 2 * M, 900000,
                  ["Your engagement team"], size=3200, bold=True, color=NAVY),
        x.textbox(4, "lead", M, 2200000, W - 2 * M, 1400000,
                  ["{{LeadPartner}}", "Lead audit partner"], size=2000),
        # PowerPoint fragments a placeholder into runs when it is edited
        # character-by-character. The builder must stitch this back together.
        x.split_run_textbox(5, "office", M, 3900000, W - 2 * M, 800000,
                            ["Local office: ", "{{Ci", "ty", "}}"], size=2000),
        x.textbox(6, "date", M, H - int(M * 1.4), W - 2 * M, 457200,
                  ["Response due {{DueDate}}"], size=1400, color=GREY),
        marker(7, block_id),
    ])


# ---------------------------------------------------------------- the library
def build_slides():
    """[(block_id, slide_xml)] — one library slide per named block."""
    return [
        ("cover", title_slide(
            "cover", "{{ClientName}}",
            "Proposal for external audit services",
            "Prepared by Demo LLP  •  {{DueDate}}")),

        ("about_us", content_slide(
            "about_us", "About us",
            ["A national audit practice with 40 years of continuous service.",
             "Sector specialists embedded in every engagement team.",
             "Independent, and structured to stay that way."],
            kicker="Demo LLP — audit, tax and advisory")),

        ("our_team", content_slide(
            "our_team", "Who you will work with",
            ["{{LeadPartner}} leads the engagement and signs the opinion.",
             "A named manager on site throughout fieldwork.",
             "Continuity of team members year over year."])),

        ("approach", content_slide(
            "approach", "Our approach",
            ["Risk assessment driven by your business, not a checklist.",
             "Early identification of judgements and estimates.",
             "No surprises: issues raised as we find them.",
             "Clear, written conclusions at each phase."])),

        # conditional block — only when the answers ask for it
        ("expansion_detail", content_slide(
            "expansion_detail", "Expansion of services",
            ["Transition planning across the newly in-scope entities.",
             "A single reporting timetable covering the expanded group.",
             "Incremental effort scoped and priced separately."],
            kicker="Applies where the mandate expands beyond the current scope")),

        # a block with two variants — the engine picks one
        ("scope", content_slide(
            "scope", "Scope of work",
            ["Audit of the statutory financial statements for {{ClientName}}.",
             "Review of the interim financial information.",
             "Reporting to those charged with governance."])),

        ("scope_expansion", content_slide(
            "scope_expansion", "Scope of work — expanded",
            ["Audit of the statutory financial statements for {{ClientName}}.",
             "Audit of each newly acquired subsidiary in the expanded group.",
             "Group consolidation and component auditor instructions.",
             "Reporting to those charged with governance."])),

        ("fees", fees_slide("fees")),
        ("contacts", contacts_slide("contacts")),
    ]


def write_library(out_path):
    slides = build_slides()
    n = len(slides)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    # presentation rels: rId1 = master, rId2.. = slides (in library order)
    slide_rids = ["rId%d" % (i + 2) for i in range(n)]
    prs_rels = [("rId1", "slideMaster", "slideMasters/slideMaster1.xml")]
    prs_rels += [(slide_rids[i], "slide", "slides/slide%d.xml" % (i + 1))
                 for i in range(n)]

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", x.content_types(n))
        z.writestr("_rels/.rels", x.rels([
            ("rId1", "officeDocument", "ppt/presentation.xml")]))

        z.writestr("ppt/presentation.xml", x.presentation(slide_rids, "rId1"))
        z.writestr("ppt/_rels/presentation.xml.rels", x.rels(prs_rels))

        z.writestr("ppt/theme/theme1.xml", x.theme("Demo"))
        z.writestr("ppt/slideMasters/slideMaster1.xml", x.master("rId1"))
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", x.rels([
            ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", "theme", "../theme/theme1.xml")]))

        z.writestr("ppt/slideLayouts/slideLayout1.xml", x.layout("Blank"))
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", x.rels([
            ("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")]))

        for i, (_block_id, xml) in enumerate(slides, start=1):
            z.writestr("ppt/slides/slide%d.xml" % i, xml)
            z.writestr("ppt/slides/_rels/slide%d.xml.rels" % i, x.rels([
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")]))

    return out_path, [b for b, _ in slides]


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    argv = [a for a in sys.argv[1:] if a != "--unmarked"]
    if "--unmarked" in sys.argv:
        # Stands in for a firm's existing template: a perfectly good branded deck
        # that nobody has annotated. Proves `import` can name it from titles alone.
        MARKERS = False
        default = os.path.join(here, "tests", "fixtures", "office_template.pptx")
    else:
        default = os.path.join(here, "templates", "demo", "library.pptx")
    path, blocks = write_library(argv[0] if argv else default)
    print("wrote %s" % path)
    print("%d blocks: %s" % (len(blocks), ", ".join(blocks)))
