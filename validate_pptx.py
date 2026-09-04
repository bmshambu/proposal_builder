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
import zipfile


def _resolve(base_part, target):
    """Resolve a relationship Target relative to the part that owns the .rels."""
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

    # content types
    ct = zf.read("[Content_Types].xml").decode("utf-8", "ignore") if "[Content_Types].xml" in nameset else ""
    default_exts = set(m.lower() for m in re.findall(r'<Default[^>]*Extension="([^"]+)"', ct))
    overrides = set(re.findall(r'<Override[^>]*PartName="([^"]+)"', ct))

    def has_content_type(part):
        if "/" + part in overrides or part in overrides:
            return True
        ext = os.path.splitext(part)[1].lstrip(".").lower()
        return ext in default_exts

    # every rels: targets exist; collect rel ids per owning part
    rels_ids = {}     # part -> set(rId)
    for n in names:
        if not n.endswith(".rels"):
            continue
        owner = _part_for_rels(n)
        raw = zf.read(n).decode("utf-8", "ignore")
        ids = set()
        for m in re.finditer(r'<Relationship\b[^>]*?/>', raw):
            tag = m.group(0)
            rid = re.search(r'Id="([^"]+)"', tag)
            tgt = re.search(r'Target="([^"]+)"', tag)
            if rid:
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
