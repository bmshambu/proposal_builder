#!/usr/bin/env python3
"""
inspect_pptx.py  -  Templafy deck provenance inspector (Phase 0)

Read-only forensic inspector for PowerPoint (.pptx) files. It unzips the deck
and reports its OOXML structure so we can work out WHERE the content in a
Templafy-generated deck actually comes from (Slides Library asset IDs, data
sources, response-form answers, external systems, or literal/AI text).

WHY THIS EXISTS
    A .pptx is a ZIP of XML. A Templafy-generated deck leaves fingerprints in
    custom XML parts, content-control tags, shape alt-text/names, document
    properties and relationships. Reading those tells us which of the possible
    content sources drove the build - without anyone having to email a
    confidential template around.

PRIVACY / SAFETY
    * 100% local. Makes NO network calls. Only reads the file you point it at.
    * Read-only. Never modifies the .pptx.
    * Zero third-party dependencies (Python 3.8+ standard library only), so a
      teammate can run it on a locked-down machine with nothing to install.
    * By default it summarises structure, not body copy. Use --dump-xml only
      when you are comfortable writing the deck's custom-XML parts to disk for
      inspection, and treat that output with the same confidentiality as the
      deck itself.

USAGE
    python inspect_pptx.py "path/to/deck.pptx"
    python inspect_pptx.py "path/to/deck.pptx" --report report.md
    python inspect_pptx.py "path/to/deck.pptx" --dump-xml out_dir --show-text

    --report FILE     Also write the full report to FILE (Markdown).
    --dump-xml DIR    Save every custom XML part + docProps to DIR for review.
    --show-text       Include a short preview of placeholder text (off by
                      default so you can share the report more freely).

WHAT TO LOOK FOR IN THE OUTPUT  (interpretation guide)
    * "PROVENANCE KEYWORD SCAN" hits for templafy/assetId/dataSource/binding
        -> content is wired through Templafy Dynamics; the surrounding XML
           names the data source / asset IDs feeding each slide.
    * Custom document properties (client, date, serviceLine, author...)
        -> those values came from the response form or the user profile.
    * Shape names / alt-text containing '{' or 'binding' or 'templafy'
        -> that shape is a smart field; the JSON there is the binding config.
    * Many slide layouts / masters, or slides whose text is fully literal
        -> whole branded slides were inserted from the Slides Library.
    * Hyperlink or external relationships to SharePoint / Salesforce / a DAM
        -> content is pulled from an external system integration.
    * Body placeholders with rich literal text and NO binding markers
        -> either static library content or AI-generated narrative.
"""

import argparse
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

# Keywords that betray how content was sourced. Case-insensitive raw-text scan.
PROVENANCE_KEYWORDS = [
    "templafy", "assetid", "datasource", "data-source", "smartfield",
    "smart-field", "binding", "dynamics", "responseform", "response-form",
    "sharepoint", "salesforce", "crm", "dam", "profile", "resource",
    "placeholder", "connector",
]

# Content namespaces we care about (matched namespace-agnostically via {*}).
TEXT_PREVIEW_LIMIT = 120


def _local(tag):
    """Strip the XML namespace from a tag, e.g. '{..}ph' -> 'ph'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter(elem, name):
    """Iterate descendants whose local-name == name, ignoring namespace."""
    for e in elem.iter():
        if _local(e.tag) == name:
            yield e


def _attr(elem, name):
    """Get an attribute by local-name, ignoring namespace."""
    for k, v in elem.attrib.items():
        if _local(k) == name:
            return v
    return None


class Report:
    """Collects lines for stdout and an optional Markdown file."""

    def __init__(self):
        self.lines = []

    def h(self, text):
        self.lines.append("\n## " + text)

    def sub(self, text):
        self.lines.append("\n### " + text)

    def p(self, text=""):
        self.lines.append(text)

    def bullet(self, text):
        self.lines.append("- " + text)

    def render(self):
        return "\n".join(self.lines) + "\n"


def natural_sort_key(name):
    """Sort slide1, slide2, slide10 in human order."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def inspect(path, report, dump_xml=None, show_text=False):
    if not zipfile.is_zipfile(path):
        report.p("ERROR: not a valid .pptx (zip) file: %s" % path)
        return

    zf = zipfile.ZipFile(path)
    names = zf.namelist()

    # ---- 1. Package overview -------------------------------------------------
    report.h("1. Package overview")
    report.p("File: `%s`" % os.path.basename(path))
    report.p("Total parts in package: %d" % len(names))

    groups = defaultdict(list)
    for n in names:
        if n.startswith("ppt/slides/slide"):
            groups["slides"].append(n)
        elif n.startswith("ppt/slideLayouts/slideLayout"):
            groups["layouts"].append(n)
        elif n.startswith("ppt/slideMasters/slideMaster"):
            groups["masters"].append(n)
        elif n.startswith("ppt/media/"):
            groups["media"].append(n)
        elif n.startswith("customXml/"):
            groups["customXml"].append(n)
        elif n.startswith("ppt/embeddings/"):
            groups["embeddings"].append(n)
        elif n.startswith("docProps/"):
            groups["docProps"].append(n)
    slides = sorted(
        [n for n in groups["slides"] if re.search(r"slide\d+\.xml$", n)],
        key=natural_sort_key,
    )
    report.bullet("Slides: %d" % len(slides))
    report.bullet("Slide layouts: %d" % len(groups["layouts"]))
    report.bullet("Slide masters: %d" % len(groups["masters"]))
    report.bullet("Media files: %d" % len(groups["media"]))
    report.bullet("Custom XML parts: %d   <-- Templafy metadata often lives here"
                  % len(groups["customXml"]))
    report.bullet("Embedded objects: %d" % len(groups["embeddings"]))

    # ---- 2. Provenance keyword scan (the headline result) --------------------
    report.h("2. Provenance keyword scan")
    report.p("Case-insensitive scan across every XML part. Hits point to how "
             "content was sourced.")
    keyword_hits = defaultdict(list)  # keyword -> [parts]
    raw_cache = {}
    for n in names:
        if not (n.endswith(".xml") or n.endswith(".rels")):
            continue
        try:
            raw = zf.read(n).decode("utf-8", errors="ignore")
        except Exception:
            continue
        raw_cache[n] = raw
        low = raw.lower()
        for kw in PROVENANCE_KEYWORDS:
            if kw in low:
                keyword_hits[kw].append(n)
    if keyword_hits:
        for kw in sorted(keyword_hits, key=lambda k: -len(keyword_hits[k])):
            parts = keyword_hits[kw]
            sample = ", ".join(os.path.basename(p) for p in parts[:6])
            more = "" if len(parts) <= 6 else " (+%d more)" % (len(parts) - 6)
            report.bullet("`%s` -> %d part(s): %s%s"
                          % (kw, len(parts), sample, more))
    else:
        report.p("No provenance keywords found. Content is likely literal text "
                 "(static library slides or AI-generated), not live bindings.")

    # ---- 3. Custom XML parts (Templafy config) -------------------------------
    report.h("3. Custom XML parts")
    if groups["customXml"]:
        for n in sorted(groups["customXml"]):
            raw = raw_cache.get(n) or zf.read(n).decode("utf-8", "ignore")
            report.sub(os.path.basename(n))
            report.p("Size: %d bytes" % len(raw))
            # show root element + any namespaces (reveals the schema/vendor)
            try:
                root = ET.fromstring(raw)
                report.p("Root element: `%s`" % _local(root.tag))
            except Exception:
                pass
            ns_found = set(re.findall(r'xmlns[:\w]*="([^"]+)"', raw))
            for ns in sorted(ns_found):
                report.bullet("namespace: %s" % ns)
            preview = raw.strip().replace("\n", " ")[:400]
            report.p("Preview: `%s`" % preview)
    else:
        report.p("None. (A Templafy-built deck usually has some; a hand-made "
                 "template may have none.)")

    # ---- 4. Document properties (form answers / profile land here) -----------
    report.h("4. Document properties")
    for propfile in ("docProps/core.xml", "docProps/app.xml",
                     "docProps/custom.xml"):
        if propfile in names:
            report.sub(os.path.basename(propfile))
            raw = raw_cache.get(propfile) or zf.read(propfile).decode("utf-8", "ignore")
            try:
                root = ET.fromstring(raw)
                for el in root.iter():
                    tag = _local(el.tag)
                    text = (el.text or "").strip()
                    name = _attr(el, "name")  # custom.xml uses <property name=..>
                    if name:
                        # value is in a child element
                        val = ""
                        for child in el:
                            val = (child.text or "").strip()
                        report.bullet("%s = %s" % (name, val))
                    elif text and tag not in ("Properties", "coreProperties"):
                        report.bullet("%s: %s" % (tag, text[:200]))
            except Exception as e:
                report.p("(could not parse: %s)" % e)

    # ---- 5. Per-slide structure: placeholders, bindings, images --------------
    report.h("5. Per-slide structure")
    report.p("For each slide: placeholders (type/idx), smart-field markers on "
             "shapes, and linked images.")
    for sn in slides:
        raw = raw_cache.get(sn) or zf.read(sn).decode("utf-8", "ignore")
        try:
            root = ET.fromstring(raw)
        except Exception:
            continue
        report.sub(os.path.basename(sn))

        # placeholders
        phs = []
        for ph in _iter(root, "ph"):
            ptype = _attr(ph, "type") or "body"
            pidx = _attr(ph, "idx")
            phs.append("%s%s" % (ptype, ("#" + pidx) if pidx else ""))
        report.bullet("Placeholders: %s" % (", ".join(phs) if phs else "none"))

        # smart-field / binding markers on shape names + alt-text (descr)
        markers = []
        for cnv in _iter(root, "cNvPr"):
            nm = _attr(cnv, "name") or ""
            descr = _attr(cnv, "descr") or ""
            for label, val in (("name", nm), ("alt-text", descr)):
                if val and ("{" in val or "templafy" in val.lower()
                            or "binding" in val.lower()):
                    markers.append("%s=%s" % (label, val[:120]))
        if markers:
            for m in markers:
                report.bullet("  SMART FIELD -> %s" % m)

        # content controls (sdt) count - another binding mechanism
        sdt_count = sum(1 for _ in _iter(root, "sdt"))
        if sdt_count:
            report.bullet("  Content controls (sdt): %d" % sdt_count)

        # text stats (+ optional preview)
        texts = [(t.text or "") for t in _iter(root, "t")]
        total_chars = sum(len(x) for x in texts)
        report.bullet("  Text runs: %d, total chars: %d"
                      % (len(texts), total_chars))
        if show_text and texts:
            joined = " ".join(x for x in texts if x.strip())
            report.bullet("  Text preview: %s" % joined[:TEXT_PREVIEW_LIMIT])

        # linked images via the slide's .rels
        rels = "ppt/slides/_rels/%s.rels" % os.path.basename(sn)
        if rels in names:
            rraw = raw_cache.get(rels) or zf.read(rels).decode("utf-8", "ignore")
            imgs = re.findall(r'Target="([^"]*media[^"]*)"', rraw)
            ext = re.findall(r'Target="(https?://[^"]+)"', rraw)
            if imgs:
                report.bullet("  Images: %s"
                              % ", ".join(os.path.basename(i) for i in imgs))
            if ext:
                report.bullet("  EXTERNAL links: %s" % ", ".join(ext[:5]))

    # ---- 6. Theme / brand colors --------------------------------------------
    report.h("6. Theme colors (branding)")
    theme = "ppt/theme/theme1.xml"
    if theme in names:
        raw = raw_cache.get(theme) or zf.read(theme).decode("utf-8", "ignore")
        try:
            root = ET.fromstring(raw)
            for scheme in _iter(root, "clrScheme"):
                report.p("Color scheme: `%s`" % (_attr(scheme, "name") or ""))
                for child in scheme:
                    slot = _local(child.tag)
                    for c in child:
                        val = _attr(c, "val") or _attr(c, "lastClr")
                        if val:
                            report.bullet("%s: #%s" % (slot, val))
                break
        except Exception:
            pass

    # ---- 7. Media inventory --------------------------------------------------
    report.h("7. Media inventory")
    if groups["media"]:
        for n in sorted(groups["media"], key=natural_sort_key):
            try:
                size = zf.getinfo(n).file_size
            except KeyError:
                size = 0
            report.bullet("%s (%d KB)" % (os.path.basename(n), size // 1024))
    else:
        report.p("No embedded media.")

    # ---- optional: dump custom XML + docProps to disk ------------------------
    if dump_xml:
        os.makedirs(dump_xml, exist_ok=True)
        dumped = 0
        for n in names:
            if n.startswith("customXml/") or n.startswith("docProps/"):
                out = os.path.join(dump_xml, n.replace("/", "__"))
                with open(out, "wb") as fh:
                    fh.write(zf.read(n))
                dumped += 1
        report.h("Dumped files")
        report.p("Wrote %d part(s) to `%s` for manual inspection." % (dumped, dump_xml))

    zf.close()


def main():
    ap = argparse.ArgumentParser(
        description="Read-only OOXML inspector to trace where a Templafy deck's "
                    "content comes from. Makes no network calls.")
    ap.add_argument("pptx", help="Path to the .pptx file to inspect")
    ap.add_argument("--report", help="Also write the report to this .md file")
    ap.add_argument("--dump-xml", help="Directory to save custom XML + docProps")
    ap.add_argument("--show-text", action="store_true",
                    help="Include short placeholder-text previews")
    args = ap.parse_args()

    if not os.path.isfile(args.pptx):
        sys.exit("File not found: %s" % args.pptx)

    report = Report()
    report.p("# PPTX inspection report")
    report.p("Source: `%s`" % os.path.abspath(args.pptx))
    inspect(args.pptx, report, dump_xml=args.dump_xml, show_text=args.show_text)

    text = report.render()
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("\n[report written to %s]" % args.report)


if __name__ == "__main__":
    main()
