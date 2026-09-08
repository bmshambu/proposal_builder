"""The assembler: selected blocks -> a valid `.pptx`.

v2's assembly is structurally *simpler* than v1's, and deliberately so. Every
block comes from one library deck, so the masters, layouts, theme and media the
output needs are already in the package — there is no closure to chase, no
foreign master to graft, no media to rename. What is left is exactly the part
PowerPoint is strict about:

  * emit one fresh slide part per selected block (a block may appear twice),
  * rewrite `presentation.xml` + its rels with unique ids,
  * keep `[Content_Types].xml` in step with the parts that actually exist,
  * keep each slide's rels to what the slide really references.

Everything here traces to a repair dialog we hit in v1 (docs/v1-learnings.md §2).
"""
import os
import re
import zipfile

from . import bindings, ooxml, placeholders
from .library import Library
from .rules import Rules

SLIDE_REL_TYPE = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                  "relationships/slide")


class AssemblyError(Exception):
    pass


def _is_slide_rel(tag):
    target = ooxml.rel_attr(tag, "Target") or ""
    return bool(re.search(r'/slide"', tag)) or ("slides/slide" in target
                                                and "slideLayout" not in tag)


def _notes_parts(lib):
    """notesSlides are dropped: a notes part back-references its slide, and a
    block used twice would leave two slides pointing at one notes part. Speaker
    notes are not part of the MVP contract — see build()'s return value, which
    reports how many were dropped."""
    return {n for n in lib.names if n.startswith("ppt/notesSlides/")}


def build(library_path, rules, answers, out_path, data_sources=None,
          strict=False, block_map=None):
    """Assemble a deck. Returns a build report (dict).

    strict=True refuses to write a deck that still contains an unfilled
    placeholder — for CI. Interactively the deck is written and the gaps are
    reported, so an author can see them on the slide.

    `block_map` is the `blocks.json` sidecar: slide part -> block id, so a deck
    nobody has annotated can still be addressed by name.
    """
    if isinstance(rules, (str, os.PathLike)):
        rules = Rules.load(rules)

    with Library(library_path, block_map=block_map) as lib:
        problems = rules.check_against(lib)
        if problems:
            raise AssemblyError("rules do not match the library:\n  - "
                                + "\n  - ".join(problems))

        selection, trace = rules.select(answers)
        if not selection:
            raise AssemblyError("no blocks selected — the deck would be empty")
        values, unresolved = bindings.resolve(rules.placeholders, answers, data_sources)

        # ---- prepare one output slide per selected block occurrence
        notes = _notes_parts(lib)
        slides = []                       # [{part, xml, rels, block, source}]
        for i, (block_id, slide_id) in enumerate(selection, start=1):
            src = lib.block(slide_id)
            xml = lib.read(src.part)
            xml = placeholders.strip_marker(xml)      # marker never ships
            xml = ooxml.strip_custdata(xml)           # defensive: tag refs dangle
            xml = placeholders.apply(xml, values)

            src_rels_part = ooxml.rels_part_for(src.part)
            src_rels = lib.read(src_rels_part) if src_rels_part in lib.names else ""
            out_part = "ppt/slides/slide%d.xml" % i
            slides.append({
                "part": out_part,
                "xml": xml,
                "rels": ooxml.keep_referenced_rels(xml, src_rels),
                "block": block_id,
                "source": os.path.basename(src.part),
            })

        # ---- presentation.xml + rels: fresh, unique ids
        keep_rels = [t for t in ooxml.rel_tags(lib.presentation_rels)
                     if not _is_slide_rel(t)]
        minter = ooxml.RelIdMinter(ooxml.rel_ids(lib.presentation_rels))
        sld_ids, new_slide_rels = [], []
        for i, s in enumerate(slides):
            rid = minter.mint()
            new_slide_rels.append(
                '<Relationship Id="%s" Type="%s" Target="slides/%s"/>'
                % (rid, SLIDE_REL_TYPE, os.path.basename(s["part"])))
            sld_ids.append('<p:sldId id="%d" r:id="%s"/>' % (256 + i, rid))

        prs = _replace_sld_id_lst(lib.presentation, "".join(sld_ids))
        prs_rels = ooxml.build_rels(keep_rels + new_slide_rels)

        # ---- content types: declare exactly the parts we write
        ct = lib.content_types
        for part in lib.slide_parts:
            ct = ooxml.drop_override(ct, part)
        for part in notes:
            ct = ooxml.drop_override(ct, part)
        ct = ooxml.add_overrides(ct, [(s["part"], ooxml.CT_SLIDE) for s in slides])

        # ---- write
        skip = set(lib.slide_parts) | notes
        skip |= {ooxml.rels_part_for(p) for p in skip}
        rewritten = {"ppt/presentation.xml": prs,
                     "ppt/_rels/presentation.xml.rels": prs_rels,
                     "[Content_Types].xml": ct}

        out_path = str(out_path)
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        written = set()
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
            for info in lib.zf.infolist():
                name = info.filename
                if name in skip or name in written:
                    continue
                if name in rewritten:
                    out.writestr(name, rewritten.pop(name))
                else:
                    out.writestr(info, lib.read_bytes(name))
                written.add(name)
            for name, body in rewritten.items():      # library lacked the part
                out.writestr(name, body)
            for s in slides:
                out.writestr(s["part"], s["xml"])
                out.writestr(ooxml.rels_part_for(s["part"]), s["rels"])

    leftover = sorted({n for s in slides for n in placeholders.find(s["xml"])})
    if strict and (leftover or unresolved):
        os.remove(out_path)
        raise AssemblyError(
            "strict build failed — unfilled placeholders: %s%s"
            % (", ".join(leftover) or "none",
               ("; unresolved bindings: " + "; ".join(unresolved)) if unresolved else ""))

    return {
        "out": out_path,
        "slides": len(slides),
        "order": [{"block": s["block"], "source": s["source"],
                   "part": os.path.basename(s["part"])} for s in slides],
        "values": values,
        "unresolved_bindings": unresolved,
        "unfilled_placeholders": leftover,
        "notes_dropped": len(notes),
        "trace": trace,
    }


def build_template(template, answers, out_path, strict=False):
    """Build from a `Template` folder — the normal entry point.

    Everything a template needs (library, sidecar, rules, data stubs) comes out
    of its folder, so adding a template is a file operation, not a code change.
    """
    return build(template.library_path, template.load_rules(), answers, out_path,
                 data_sources=template.data_sources(), strict=strict,
                 block_map=template.block_map)


def _replace_sld_id_lst(prs_xml, body):
    """Swap the slide-id list, whatever shape presentation.xml uses."""
    if re.search(r'<p:sldIdLst\s*/>', prs_xml):
        return re.sub(r'<p:sldIdLst\s*/>', '<p:sldIdLst>%s</p:sldIdLst>' % body, prs_xml)
    if re.search(r'<p:sldIdLst\b', prs_xml):
        return re.sub(r'<p:sldIdLst\b[^>]*>.*?</p:sldIdLst>',
                      '<p:sldIdLst>%s</p:sldIdLst>' % body, prs_xml, flags=re.DOTALL)
    # no list at all: it must sit immediately after the master list
    return re.sub(r'(</p:sldMasterIdLst>)',
                  r'\1<p:sldIdLst>%s</p:sldIdLst>' % body, prs_xml, count=1)
