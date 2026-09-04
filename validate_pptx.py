#!/usr/bin/env python3
"""
validate_pptx.py  -  find what makes PowerPoint say "found a problem / Repair".

A .pptx can be a valid ZIP with valid XML yet still be rejected by PowerPoint if
a relationship is broken. This checks the things PowerPoint is strict about:

  1. every r:id / r:embed / r:link used in a part's XML has a matching entry in
     that part's .rels                          (missing rel  -> repair)
  2. every internal relationship Target points to a part that exists in the file
                                                 (dangling part -> repair)
  3. every part has a content type (Override or Default-by-extension)
  4. every <p:sldId r:id> in presentation.xml exists in presentation.xml.rels
  5. no duplicate part names in the package

Read-only, standard library only, no network. Run it on a rebuilt deck that
won't open:

    python validate_pptx.py data/output/test_combo_..._rebuilt.pptx
"""
import os
import re
import sys
import xml.parsers.expat
import zipfile


def _resolve(base_part, target):
    """Resolve a relationship Target to a package part name.
    A target starting with '/' is package-absolute (relative to the zip root);
    otherwise it's relative to the directory of the part that owns the .rels."""
    if target.startswith("/"):
        return target[1:]
    base_dir = os.path.dirname(base_part)
    return os.path.normpath(os.path.join(base_dir, target)).replace("\\", "/")


def _part_for_rels(rels_name):
    """'ppt/slides/_rels/slide1.xml.rels' -> 'ppt/slides/slide1.xml'."""
    d = os.path.dirname(os.path.dirname(rels_name))   # strip '/_rels'
    base = os.path.basename(rels_name)[:-len(".rels")]
    return (d + "/" + base) if d else base


def validate(path):
    issues = []
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    nameset = set(names)

    # duplicate parts
    seen = set()
    for n in names:
        if n in seen:
            issues.append(("duplicate-part", n, "appears more than once in the package"))
        seen.add(n)

    # XML well-formedness: a malformed slide/part makes PowerPoint say the content
    # is unreadable (XML_MALFORMED) and offer to repair. A valid ZIP with valid
    # rels can still hold a broken slide, so parse every xml/rels part.
    for n in names:
        if not (n.endswith(".xml") or n.endswith(".rels")):
            continue
        p = xml.parsers.expat.ParserCreate()
        try:
            p.Parse(zf.read(n), True)
        except xml.parsers.expat.ExpatError as e:
            issues.append(("xml-malformed", n, "not well-formed XML: %s" % e))

    # content types
    ct = zf.read("[Content_Types].xml").decode("utf-8", "ignore") if "[Content_Types].xml" in nameset else ""
    default_exts = set(m.lower() for m in re.findall(r'<Default[^>]*Extension="([^"]+)"', ct))
    overrides = set(re.findall(r'<Override[^>]*PartName="([^"]+)"', ct))

    # these part types MUST carry a specific Override — the generic ".xml" Default
    # is not enough, and PowerPoint will offer to repair if it's missing.
    _needs_override = ("ppt/slidemasters/", "ppt/slidelayouts/", "ppt/theme/",
                       "ppt/slides/", "ppt/notesslides/", "ppt/notesmasters/")

    def has_content_type(part):
        has_override = ("/" + part in overrides) or (part in overrides)
        if has_override:
            return True
        p = part.lower()
        if p.endswith(".xml") and any(p.startswith(d) for d in _needs_override):
            return False        # needs a real Override, not the xml Default
        ext = os.path.splitext(part)[1].lstrip(".").lower()
        return ext in default_exts

    # every rels: targets exist; collect rel ids per owning part
    rels_ids = {}     # part -> set(rId)
    for n in names:
        if not n.endswith(".rels"):
            continue
        owner = _part_for_rels(n)
        raw = zf.read(n).decode("utf-8", "ignore")
        ids, seen_rid = set(), set()
        for m in re.finditer(r'<Relationship\b[^>]*?/>', raw):
            tag = m.group(0)
            rid = re.search(r'Id="([^"]+)"', tag)
            tgt = re.search(r'Target="([^"]+)"', tag)
            if rid:
                if rid.group(1) in seen_rid:
                    issues.append(("duplicate-rel-id", n,
                                   "relationship Id %s used more than once" % rid.group(1)))
                seen_rid.add(rid.group(1))
                ids.add(rid.group(1))
            external = 'TargetMode="External"' in tag
            if tgt and not external:
                resolved = _resolve(owner, tgt.group(1))
                if resolved not in nameset:
                    issues.append(("dangling-target", n,
                                   "%s -> %s (missing)" % (rid.group(1) if rid else "?", resolved)))
        rels_ids[owner] = ids

    # every referenced rId in a part's XML has a rel
    for n in names:
        if not (n.endswith(".xml") and (n.startswith("ppt/slides/") or
                n.startswith("ppt/slideLayouts/") or n.startswith("ppt/slideMasters/")
                or n == "ppt/presentation.xml" or n.startswith("ppt/charts/"))):
            continue
        if n.endswith(".rels"):
            continue
        raw = zf.read(n).decode("utf-8", "ignore")
        referenced = set(re.findall(r'r:(?:id|embed|link|pict|dm|lo|qs|cs)="([^"]+)"', raw))
        have = rels_ids.get(n, set())
        for rid in referenced - have:
            issues.append(("missing-rel", n, "XML uses %s but it's not in the .rels" % rid))

    # content type for every non-rels part
    for n in names:
        if n.endswith(".rels") or n == "[Content_Types].xml":
            continue
        if not has_content_type(n):
            issues.append(("no-content-type", n, "no Override and no Default for its extension"))

    # presentation sldId r:ids exist in presentation rels
    if "ppt/presentation.xml" in nameset:
        prs = zf.read("ppt/presentation.xml").decode("utf-8", "ignore")
        prs_ids = rels_ids.get("ppt/presentation.xml", set())
        for m in re.finditer(r'<p:sldId\b[^>]*r:id="([^"]+)"', prs):
            if m.group(1) not in prs_ids:
                issues.append(("missing-slide-rel", "ppt/presentation.xml",
                               "sldId uses %s but it's not in presentation.xml.rels" % m.group(1)))
        # slide / master ids must each be unique
        for what, pat in (("sldId", r'<p:sldId\b[^>]*\bid="(\d+)"'),
                          ("sldMasterId", r'<p:sldMasterId\b[^>]*\bid="(\d+)"')):
            seen = set()
            for m in re.finditer(pat, prs):
                if m.group(1) in seen:
                    issues.append(("duplicate-id", "ppt/presentation.xml",
                                   "%s id %s used more than once" % (what, m.group(1))))
                seen.add(m.group(1))
    zf.close()
    return issues


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python validate_pptx.py <file.pptx>")
    path = sys.argv[1]
    issues = validate(path)
    print("Validating %s\n" % os.path.basename(path))
    if not issues:
        print("OK - no structural problems found. If PowerPoint still repairs it, "
              "the issue is subtler; send these details.")
        return
    print("Found %d issue(s):\n" % len(issues))
    by_kind = {}
    for kind, part, detail in issues:
        by_kind.setdefault(kind, []).append((part, detail))
    for kind, rows in by_kind.items():
        print("== %s (%d) ==" % (kind, len(rows)))
        for part, detail in rows[:40]:
            print("  %s : %s" % (part, detail))
        if len(rows) > 40:
            print("  ... +%d more" % (len(rows) - 40))
        print("")


if __name__ == "__main__":
    main()
