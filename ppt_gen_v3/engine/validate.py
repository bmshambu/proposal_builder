"""Structural validation of a .pptx - will PowerPoint open it, or offer to repair?

Vendored from v1's `validate_pptx.py`, deliberately. The original plan kept one
copy next to v1, but v2 gets deployed on its own: on the machine that matters,
the parent folder held no v1, the import silently found nothing, and an entire
70-deck merge ran with **no validation at all** while reporting success.

Decision D5 - a build that does not validate never reaches a user - cannot rest
on a file that may not be there. So the checker travels with the engine.

Keep this in step with v1's copy if that one changes; the checks themselves are
described in docs/v1-learnings.md section 2.

    from engine.validate import validate
    issues = validate("deck.pptx")     # [] means clean
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

    issues.extend(_check_master_layout_consistency(zf, nameset))
    zf.close()
    return issues


def _rels_of(zf, nameset, part):
    """[(rId, type, resolved target)] for one part's internal relationships."""
    d, base = os.path.dirname(part), os.path.basename(part)
    rels_name = ("%s/_rels/%s.rels" % (d, base)) if d else "_rels/%s.rels" % base
    if rels_name not in nameset:
        return []
    try:
        xml = zf.read(rels_name).decode("utf-8", "ignore")
    except KeyError:
        return []
    out = []
    for tag in re.findall(r'<Relationship\b[^>]*?/>', xml):
        if 'TargetMode="External"' in tag:
            continue
        rid = re.search(r'Id="([^"]+)"', tag)
        rtype = re.search(r'Type="([^"]+)"', tag)
        target = re.search(r'Target="([^"]+)"', tag)
        if rid and target:
            out.append((rid.group(1), rtype.group(1) if rtype else "",
                        _resolve(part, target.group(1))))
    return out


def _check_master_layout_consistency(zf, nameset):
    """Masters and layouts must agree about each other.

    Every individual relationship can resolve and the package still be one
    PowerPoint refuses: a layout that slides use, that names its master, but
    that the master does not list in `<p:sldLayoutIdLst>`, is an orphan. So is
    a master that exists but is not registered in presentation.xml. Neither
    shows up as a broken link, because no link is broken - the two sides simply
    disagree.
    """
    issues = []
    masters = sorted(n for n in nameset
                     if n.startswith("ppt/slideMasters/slideMaster")
                     and n.endswith(".xml"))
    layouts = sorted(n for n in nameset
                     if n.startswith("ppt/slideLayouts/slideLayout")
                     and n.endswith(".xml"))
    if not masters:
        return issues

    # which layouts each master lists
    listed = {}
    for master in masters:
        by_id = {rid: target for rid, _t, target in _rels_of(zf, nameset, master)}
        try:
            body = zf.read(master).decode("utf-8", "ignore")
        except KeyError:
            continue
        for m in re.finditer(r'<p:sldLayoutId\b[^>]*\br:id="([^"]+)"', body):
            target = by_id.get(m.group(1))
            if target is None:
                issues.append(("missing-rel", master,
                               "sldLayoutId uses %s but it's not in the .rels"
                               % m.group(1)))
            else:
                listed.setdefault(target, []).append(master)

    for layout in layouts:
        owners = listed.get(layout, [])
        if not owners:
            issues.append(("orphan-layout", layout,
                           "no slide master lists this layout in its "
                           "sldLayoutIdLst - PowerPoint will try to repair"))
        elif len(owners) > 1:
            issues.append(("shared-layout", layout,
                           "listed by %d masters (%s) - a layout belongs to one"
                           % (len(owners), ", ".join(os.path.basename(o)
                                                     for o in owners))))
        back = [t for _r, ty, t in _rels_of(zf, nameset, layout)
                if "slideMaster" in ty]
        if not back:
            issues.append(("layout-without-master", layout,
                           "the layout has no relationship to a slide master"))
        elif owners and back[0] not in owners:
            issues.append(("layout-master-mismatch", layout,
                           "points at %s but is listed by %s"
                           % (os.path.basename(back[0]),
                              os.path.basename(owners[0]))))

    # every master must be registered in presentation.xml
    if "ppt/presentation.xml" in nameset:
        registered = {t for _r, ty, t in _rels_of(zf, nameset, "ppt/presentation.xml")
                      if ty.endswith("/slideMaster")}
        for master in masters:
            if master not in registered:
                issues.append(("unregistered-master", master,
                               "not listed in presentation.xml.rels - a master "
                               "the presentation does not know about"))
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
