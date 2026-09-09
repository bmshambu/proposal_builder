#!/usr/bin/env python3
"""Merge many generated decks into one master library deck.

The bridge from v1 to v2. You have ~70 decks that Templafy produced from the
OFAT payloads; between them they contain every slide the template can emit. This
collapses them into a single `library.pptx` holding **one copy of each distinct
slide**, which is exactly the input v2's engine wants.

    python tools/build_master_deck.py data/decks/*.pptx --name firm
    python tools/build_master_deck.py data/decks --payloads data/payloads --name firm

With `--payloads`, it also works out *why* each slide appeared — which answer
put it in the deck — and writes that into `rules.json` as a proposal for the
author to confirm. That is v1's harvesting insight, but producing an editable
file rather than a black box.

Nothing is inferred about the slides themselves: every one is copied byte for
byte, with its layout, master, theme and media.

--------------------------------------------------------------------------
Slide identity
--------------------------------------------------------------------------
The same slide appears in many decks with *different text* (client name, date),
so text cannot be the key. PowerPoint stamps `<a16:creationId>` GUIDs on shapes
and those survive copying and text edits — v1 proved they match slides across
decks reliably, so they are the primary key here too. Decks without them fall
back to a structural signature plus text, which is weaker: see `--report`.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PARENT)

from engine import ooxml                                      # noqa: E402
from engine.library import Library, slide_title, slugify      # noqa: E402

# Parts we never carry into the library: speaker notes (they back-reference a
# slide), Templafy's customer-data tags, and comments.
_DROP_REL = ("notesSlide", "/tags", "comments")


# ---------------------------------------------------------------- identity
from engine.identity import slide_identity                    # noqa: E402,F401


# ---------------------------------------------------------------- packaging
class MasterBuilder:
    """Accumulates parts for the output package, de-duplicating by content.

    All the decks came out of one Templafy template, so their layouts, masters,
    themes and images are usually byte-identical. Hashing content before copying
    collapses those to a single copy instead of ~70, which is the difference
    between a sane library and an unusable one.
    """

    def __init__(self):
        self.parts = {}            # part name -> bytes
        self.ctypes = {}           # part name -> content type
        self.by_hash = {}          # sha1 -> part name (dedupe)
        self.counters = {}
        self.media_ext = set()

    def _next(self, kind, ext="xml"):
        # Verbatim copies keep their original names, so a generated name can
        # collide with one (ppt/theme/theme1.xml). Skip anything already taken.
        while True:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            candidate = self._name_for(kind, ext)
            if candidate not in self.parts:
                return candidate

    def _name_for(self, kind, ext):
        folder = {"slide": "ppt/slides/slide", "layout": "ppt/slideLayouts/slideLayout",
                  "master": "ppt/slideMasters/slideMaster", "theme": "ppt/theme/theme",
                  "media": "ppt/media/media", "chart": "ppt/charts/chart",
                  "diagram": "ppt/diagrams/data", "notesMaster": "ppt/notesMasters/notesMaster",
                  "embed": "ppt/embeddings/object"}[kind]
        return "%s%d.%s" % (folder, self.counters[kind], ext)

    def add(self, name, data, ctype):
        self.parts[name] = data
        if ctype:
            self.ctypes[name] = ctype
        if name.startswith("ppt/media/"):
            ext = os.path.splitext(name)[1].lstrip(".").lower()
            if ext:
                self.media_ext.add(ext)
        return name

    def add_deduped(self, kind, data, ctype, ext="xml"):
        """-> (part name, was_new). Identical content is stored once.

        XML parts are hashed with their creationId GUIDs and revision ids
        stripped: PowerPoint stamps fresh ones into a master or layout every
        time it saves, so two decks from one template carry masters that are
        identical in every way that matters and differ byte for byte. Hashing
        the raw bytes keeps all seventy. The bytes we *store* are the original,
        untouched - normalisation only decides sameness.
        """
        digest = hashlib.sha1(_normalise_for_hash(data) if ext == "xml"
                              else data).hexdigest()
        if digest in self.by_hash:
            return self.by_hash[digest], False
        name = self._next(kind, ext)
        self.by_hash[digest] = name
        self.add(name, data, ctype)
        return name, True


# Content types for the parts a deck can drag in that are not images: chart
# workbooks, OLE objects, embedded fonts. A part with no content type at all
# makes the package invalid.
_FALLBACK_CT = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "bin": "application/vnd.openxmlformats-officedocument.oleObject",
    "fntdata": "application/x-fontdata",
    "vml": "application/vnd.openxmlformats-officedocument.vmlDrawing",
}

_VOLATILE = [
    re.compile(rb'<[a-zA-Z0-9]+:creationId[^>]*/>'),
    re.compile(rb'\s(?:val|id)="\{[0-9A-Fa-f-]{36}\}"'),
    re.compile(rb'\srevision="\d+"'),
    re.compile(rb'\smodId="\d+"'),
]


def _natural(name):
    """Sort slideLayout2 before slideLayout10."""
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def _normalise_for_hash(data):
    """Strip the bits PowerPoint re-stamps on every save, for comparison only."""
    for pattern in _VOLATILE:
        data = pattern.sub(b"", data)
    return data


def _rel_target(owner, part):
    """A relationship Target, written the way PowerPoint writes them.

    Package-absolute targets ("/ppt/slides/slide1.xml") are legal, and Templafy
    emits them, so the merge used them — they remove any chance of getting a
    "../" wrong. But PowerPoint's own files are relative throughout, and a
    package it will not open is not the place to be relying on a tolerance we
    cannot test. Relative it is; the arithmetic is one call.
    """
    rel = os.path.relpath(part, os.path.dirname(owner))
    return rel.replace("\\", "/")


def _rebuild_rels(entries):
    tags = ['<Relationship Id="%s" Type="%s" Target="%s"%s/>'
            % (rid, rtype, target, ' TargetMode="External"' if external else "")
            for rid, rtype, target, external in entries]
    return ooxml.build_rels(tags).encode("utf-8")


class Merger:
    def __init__(self, verbose=False):
        self.out = MasterBuilder()
        self.layout_map = {}       # (source deck, layout part) -> output part
        self.seen = {}             # identity -> block record
        self.order = []            # identities, in first-seen order
        self.verbose = verbose
        self.id_methods = {}
        self.id_notes = {}         # why a stronger identity was unavailable
        self.base_presentation = None
        self.base_ct = ""
        self.masters = []          # output master parts, in order added
        self.kept_prs_rels = []    # presentation rels carried over untouched

    # -- closure ------------------------------------------------------
    def _import_closure(self, lib, part, kind_of):
        """Copy `part` and everything it depends on. -> output part name.

        Used for layouts and masters, whose closure reaches the theme, the
        master's other layouts and their media. Missing any of it dangles a
        relationship, which is a repair dialog.
        """
        cached = self.layout_map.get((lib.path, part))
        if cached:
            return cached

        data = lib.read_bytes(part)
        kind = kind_of(part)
        ctype = ooxml.ctype_for(lib.content_types, part) or ooxml.infer_ctype(part)
        # Keep the real extension. A chart drags in an embedded workbook
        # (.xlsx) and an OLE object is a .bin; renaming those to .xml gives
        # PowerPoint a part whose extension, content type and actual bytes all
        # disagree, and it stops reading the file.
        ext = os.path.splitext(part)[1].lstrip(".").lower() or "xml"
        name, is_new = self.out.add_deduped(kind, data, ctype, ext)
        self.layout_map[(lib.path, part)] = name
        if not is_new:
            return name
        if kind == "master":
            self.masters.append(name)

        rels_part = ooxml.rels_part_for(part)
        entries = []
        if rels_part in lib.names:
            for tag in ooxml.rel_tags(lib.read(rels_part)):
                rid = ooxml.rel_attr(tag, "Id")
                rtype = ooxml.rel_attr(tag, "Type") or ""
                target = ooxml.rel_attr(tag, "Target") or ""
                if 'TargetMode="External"' in tag:
                    entries.append((rid, rtype, target, True))
                    continue
                if any(d in rtype or d in target for d in _DROP_REL):
                    continue
                dep = ooxml.resolve_part(part, target)
                if dep not in lib.names:
                    continue
                if "/media/" in dep:
                    new_dep = self._import_media(lib, dep)
                else:
                    new_dep = self._import_closure(lib, dep, kind_of)
                entries.append((rid, rtype, _rel_target(name, new_dep), False))
        self.out.add(ooxml.rels_part_for(name), _rebuild_rels(entries), None)
        return name

    def _import_media(self, lib, part):
        ext = os.path.splitext(part)[1].lstrip(".").lower() or "bin"
        ctype = ooxml.ctype_for(lib.content_types, part)
        name, _new = self.out.add_deduped("media", lib.read_bytes(part), ctype, ext)
        return name

    # -- slides -------------------------------------------------------
    def _kind_of(self, part):
        """Which family a part belongs to, so it keeps a sensible home.

        A chart or a SmartArt diagram brings its own little tree (colours,
        style, an embedded workbook). Filing those under ppt/embeddings/
        works by luck at best; keep each where PowerPoint expects it.
        """
        low = part.lower()
        if "/slidelayouts/" in low:
            return "layout"
        if "/slidemasters/" in low:
            return "master"
        if "/notesmasters/" in low:
            return "notesMaster"
        if "/theme/" in low:
            return "theme"
        if "/media/" in low:
            return "media"
        if "/charts/" in low:
            return "chart"
        if "/diagrams/" in low:
            return "diagram"
        return "embed"

    def _import_slide(self, lib, part, source_label):
        xml = lib.read(part)
        xml = ooxml.strip_custdata(xml)        # Templafy tag refs we do not carry

        rels_part = ooxml.rels_part_for(part)
        src_rels = lib.read(rels_part) if rels_part in lib.names else ""
        # keep only what the slide actually references (plus its layout), THEN
        # re-point what survives — so we never copy media for a dropped rel
        kept = ooxml.keep_referenced_rels(xml, src_rels)

        entries, layout_out = [], None
        for tag in ooxml.rel_tags(kept):
            rid = ooxml.rel_attr(tag, "Id")
            rtype = ooxml.rel_attr(tag, "Type") or ""
            target = ooxml.rel_attr(tag, "Target") or ""
            if 'TargetMode="External"' in tag:
                entries.append((rid, rtype, target, True))
                continue
            if any(d in rtype or d in target for d in _DROP_REL):
                continue
            dep = ooxml.resolve_part(part, target)
            if dep not in lib.names:
                continue
            if "/media/" in dep:
                new_dep = self._import_media(lib, dep)
            else:
                new_dep = self._import_closure(lib, dep, self._kind_of)
                if "slideLayout" in rtype:
                    layout_out = new_dep
            entries.append((rid, rtype, _rel_target("ppt/slides/s.xml", new_dep), False))

        name = self.out._next("slide")
        self.out.add(name, xml.encode("utf-8"), ooxml.CT_SLIDE)
        self.out.add(ooxml.rels_part_for(name), _rebuild_rels(entries), None)
        return name, layout_out

    # -- the merge ----------------------------------------------------
    def _copy_verbatim(self, lib, part, seen=None):
        """Copy a part and everything it needs, keeping the original names.

        Used for the presentation's own attachments — embedded fonts, the
        handout and notes masters, presProps and friends. They are referenced
        from presentation.xml by relationship ids we do not rewrite, so the
        safest thing is to leave part names and ids exactly as they were.
        """
        seen = seen if seen is not None else set()
        if part in seen or part not in lib.names:
            return
        seen.add(part)
        self.out.add(part, lib.read_bytes(part),
                     ooxml.ctype_for(lib.content_types, part)
                     or ooxml.infer_ctype(part))
        rels_part = ooxml.rels_part_for(part)
        if rels_part not in lib.names:
            return
        entries = []
        for tag in ooxml.rel_tags(lib.read(rels_part)):
            rid = ooxml.rel_attr(tag, "Id")
            rtype = ooxml.rel_attr(tag, "Type") or ""
            target = ooxml.rel_attr(tag, "Target") or ""
            if 'TargetMode="External"' in tag:
                entries.append((rid, rtype, target, True))
                continue
            dep = ooxml.resolve_part(part, target)
            if dep not in lib.names:
                continue
            self._copy_verbatim(lib, dep, seen)
            entries.append((rid, rtype, target, False))   # unchanged: same names
        self.out.add(rels_part, _rebuild_rels(entries), None)

    def _carry_presentation_parts(self, lib):
        """Keep every presentation relationship that is not a slide or master.

        presentation.xml refers to more than its slides: embedded fonts, a
        handout master, presProps, custom shows. Templafy writes those ids as
        GUIDs, and the body of presentation.xml is carried over as-is, so
        rebuilding the .rels from scratch left eight of those ids pointing at
        nothing — which is what stopped PowerPoint reading the file.

        Slides and masters are re-numbered by the merge, so those get fresh
        ids; everything else keeps its id, its target and its part name.
        """
        for tag in ooxml.rel_tags(lib.presentation_rels):
            rid = ooxml.rel_attr(tag, "Id")
            rtype = ooxml.rel_attr(tag, "Type") or ""
            target = ooxml.rel_attr(tag, "Target") or ""
            if not rid:
                continue
            if rtype.endswith("/slide") or rtype.endswith("/slideMaster"):
                continue                       # re-created by the merge
            if 'TargetMode="External"' in tag:
                self.kept_prs_rels.append((rid, rtype, target, True))
                continue
            dep = ooxml.resolve_part("ppt/presentation.xml", target)
            if dep not in lib.names:
                continue
            self._copy_verbatim(lib, dep)
            self.kept_prs_rels.append((rid, rtype, target, False))

    def add_deck(self, path, label=None):
        """Fold one deck in. -> [(identity, is_new, block_title)]"""
        label = label or os.path.splitext(os.path.basename(path))[0]
        touched = []
        previous = None            # the slide before this one, in this deck
        with Library(path) as lib:
            if self.base_presentation is None:
                self.base_presentation = lib.presentation
                self.base_ct = lib.content_types
                self._carry_presentation_parts(lib)
            for part in lib.slide_parts:
                raw = lib.read_bytes(part)
                identity, how = slide_identity(raw, self.id_notes)
                self.id_methods[how] = self.id_methods.get(how, 0) + 1
                if identity in self.seen:
                    self.seen[identity]["decks"].append(label)
                    touched.append((identity, False, None))
                    previous = identity
                    continue
                out_part, _layout = self._import_slide(lib, part, label)
                title = slide_title(lib.read(part))
                self.seen[identity] = {"part": out_part, "title": title,
                                       "decks": [label], "first_deck": label,
                                       "source_slide": os.path.basename(part),
                                       "how": how, "after": previous}
                self.order.append(identity)
                previous = identity
                touched.append((identity, True, title))
        return touched

    # -- output -------------------------------------------------------
    def _finalise_masters(self):
        """Rebuild every master's layout list from the layouts that actually
        point at it.

        A master's `<p:sldLayoutIdLst>` and its .rels come from whichever deck
        introduced it. Later decks add layouts that attach to that same master —
        but the master was already imported, so its list was never extended. The
        result is a layout that slides use, that names a master, and that the
        master does not list: an orphan. PowerPoint repairs the file, then gives
        up on it, and none of the relationship checks notice because every
        individual link resolves.

        Deriving the list from the back-references makes the two sides agree by
        construction rather than by luck.
        """
        masters = [p for p in self.out.parts
                   if p.startswith("ppt/slideMasters/") and p.endswith(".xml")]
        layouts = [p for p in self.out.parts
                   if p.startswith("ppt/slideLayouts/") and p.endswith(".xml")]
        if not masters:
            return

        owned, orphans = {m: [] for m in masters}, []
        for layout in sorted(layouts, key=_natural):
            rels_part = ooxml.rels_part_for(layout)
            target = None
            for tag in ooxml.rel_tags(self.out.parts.get(rels_part, b"").decode(
                    "utf-8", "ignore")):
                if "slideMaster" in (ooxml.rel_attr(tag, "Type") or ""):
                    target = (ooxml.rel_attr(tag, "Target") or "").lstrip("/")
                    break
            if target in owned:
                owned[target].append(layout)
            else:
                orphans.append(layout)
        for layout in orphans:
            # a layout whose master did not survive still has to belong to one
            owned[masters[0]].append(layout)
            self._repoint_layout(layout, masters[0])

        layout_id = 2147483649
        for master in masters:
            rels_part = ooxml.rels_part_for(master)
            kept, used_ids = [], set()
            for tag in ooxml.rel_tags(self.out.parts.get(rels_part, b"").decode(
                    "utf-8", "ignore")):
                if "slideLayout" in (ooxml.rel_attr(tag, "Type") or ""):
                    continue                    # rebuilt below
                kept.append(tag)
                used_ids.add(ooxml.rel_attr(tag, "Id"))

            minter = ooxml.RelIdMinter(used_ids)
            entries, ids = [], []
            for layout in owned[master]:
                rid = minter.mint()
                ids.append(rid)
                entries.append(
                    '<Relationship Id="%s" Type="http://schemas.openxmlformats'
                    '.org/officeDocument/2006/relationships/slideLayout" '
                    'Target="%s"/>' % (rid, _rel_target(master, layout)))
            self.out.parts[rels_part] = ooxml.build_rels(kept + entries).encode("utf-8")

            body = self.out.parts[master].decode("utf-8", "ignore")
            listing = "".join('<p:sldLayoutId id="%d" r:id="%s"/>'
                              % (layout_id + n, rid) for n, rid in enumerate(ids))
            layout_id += len(ids)
            if re.search(r'<p:sldLayoutIdLst\b', body):
                body = re.sub(r'<p:sldLayoutIdLst\b.*?</p:sldLayoutIdLst>',
                              '<p:sldLayoutIdLst>%s</p:sldLayoutIdLst>' % listing,
                              body, flags=re.DOTALL)
                body = re.sub(r'<p:sldLayoutIdLst\s*/>',
                              '<p:sldLayoutIdLst>%s</p:sldLayoutIdLst>' % listing,
                              body)
            else:
                body = body.replace('</p:cSld>',
                                    '</p:cSld><p:sldLayoutIdLst>%s</p:sldLayoutIdLst>'
                                    % listing, 1)
            self.out.parts[master] = body.encode("utf-8")

    def _repoint_layout(self, layout, master):
        rels_part = ooxml.rels_part_for(layout)
        tags = []
        for tag in ooxml.rel_tags(self.out.parts.get(rels_part, b"").decode(
                "utf-8", "ignore")):
            if "slideMaster" in (ooxml.rel_attr(tag, "Type") or ""):
                rid = ooxml.rel_attr(tag, "Id")
                tag = ('<Relationship Id="%s" Type="http://schemas.openxmlformats'
                       '.org/officeDocument/2006/relationships/slideMaster" '
                       'Target="%s"/>' % (rid, _rel_target(layout, master)))
            tags.append(tag)
        self.out.parts[rels_part] = ooxml.build_rels(tags).encode("utf-8")

    def write(self, out_path):
        self._finalise_masters()
        slides = [self.seen[i]["part"] for i in self.order]
        if not slides:
            raise SystemExit("no slides found in any deck")

        minter = ooxml.RelIdMinter(rid for rid, _t, _g, _e in self.kept_prs_rels)
        prs_entries, sld_ids = list(self.kept_prs_rels), []
        base = ("http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships/")
        for master in self.masters:
            rid = minter.mint()
            prs_entries.append((rid, base + "slideMaster", _rel_target("ppt/presentation.xml", master), False))
        master_ids = "".join(
            '<p:sldMasterId id="%d" r:id="%s"/>' % (2147483648 + n, e[0])
            for n, e in enumerate(prs_entries))
        for n, slide in enumerate(slides):
            rid = minter.mint()
            prs_entries.append((rid, base + "slide", _rel_target("ppt/presentation.xml", slide), False))
            sld_ids.append('<p:sldId id="%d" r:id="%s"/>' % (256 + n, rid))
        prs = self.base_presentation
        prs = re.sub(r'<p:notesMasterIdLst\b.*?</p:notesMasterIdLst>', '', prs,
                     flags=re.DOTALL)
        prs = re.sub(r'<p:notesMasterIdLst\b[^>]*/>', '', prs)
        prs = re.sub(r'<p:sldMasterIdLst\b.*?</p:sldMasterIdLst>',
                     '<p:sldMasterIdLst>%s</p:sldMasterIdLst>' % master_ids,
                     prs, flags=re.DOTALL)
        prs = re.sub(r'<p:sldMasterIdLst\b[^>]*/>',
                     '<p:sldMasterIdLst>%s</p:sldMasterIdLst>' % master_ids, prs)
        if "<p:sldIdLst" in prs:
            prs = re.sub(r'<p:sldIdLst\b.*?</p:sldIdLst>',
                         '<p:sldIdLst>%s</p:sldIdLst>' % "".join(sld_ids),
                         prs, flags=re.DOTALL)
            prs = re.sub(r'<p:sldIdLst\s*/>',
                         '<p:sldIdLst>%s</p:sldIdLst>' % "".join(sld_ids), prs)
        else:
            prs = prs.replace('</p:sldMasterIdLst>',
                              '</p:sldMasterIdLst><p:sldIdLst>%s</p:sldIdLst>'
                              % "".join(sld_ids), 1)

        ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
              'content-types">',
              '<Default Extension="rels" ContentType="application/vnd.'
              'openxmlformats-package.relationships+xml"/>',
              '<Default Extension="xml" ContentType="application/xml"/>']
        default_ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "gif": "image/gif", "emf": "image/x-emf", "wmf": "image/x-wmf",
                      "svg": "image/svg+xml", "bmp": "image/bmp",
                      "tif": "image/tiff", "tiff": "image/tiff"}
        for ext in sorted(self.out.media_ext):
            ct.append('<Default Extension="%s" ContentType="%s"/>'
                      % (ext, default_ct.get(ext, "application/octet-stream")))
        ct.append('<Override PartName="/ppt/presentation.xml" ContentType='
                  '"application/vnd.openxmlformats-officedocument.presentationml'
                  '.presentation.main+xml"/>')
        for part, ctype in sorted(self.out.ctypes.items()):
            if part.startswith("ppt/media/"):
                continue
            resolved = ooxml.infer_ctype(part) or ctype
            if resolved:
                ct.append('<Override PartName="/%s" ContentType="%s"/>'
                          % (part, resolved))
        # Guarantee, not hope: every part must resolve to a content type via an
        # Override or a Default, or the package is invalid. Anything still
        # uncovered gets a Default for its extension.
        declared = set(re.findall(r'PartName="/([^"]+)"', "".join(ct)))
        defaulted = set(re.findall(r'Extension="([^"]+)"', "".join(ct)))
        for part in sorted(self.out.parts):
            if part in declared:
                continue
            base = os.path.basename(part)
            ext = base.rsplit(".", 1)[1].lower() if "." in base else ""
            if not ext or ext in defaulted:
                continue
            ct.append('<Default Extension="%s" ContentType="%s"/>'
                      % (ext, _FALLBACK_CT.get(ext, "application/octet-stream")))
            defaulted.add(ext)
        ct.append('</Types>')

        root_rels = _rebuild_rels([
            ("rId1", base + "officeDocument", "ppt/presentation.xml", False)])

        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(ct))
            z.writestr("_rels/.rels", root_rels)
            z.writestr("ppt/presentation.xml", prs)
            z.writestr("ppt/_rels/presentation.xml.rels", _rebuild_rels(prs_entries))
            for part, data in sorted(self.out.parts.items()):
                z.writestr(part, data)
        return out_path

    def blocks(self):
        """[(output part, title, decks it appeared in, how it was identified)]"""
        return [(self.seen[i]["part"], self.seen[i]["title"],
                 self.seen[i]["decks"], self.seen[i]["how"]) for i in self.order]

    def anchors(self, block_ids):
        """block id -> the id of the block it followed in its source deck.

        A conditional block has to say where it slots back in, or the builder
        can only append it. The generated decks already show where each slide
        sat, so we walk back from there.

        The anchor must be present *whenever this block is*, or it vanishes
        exactly when it is needed — so we accept the nearest predecessor whose
        set of decks is a superset of this block's. That keeps two conditional
        slides that always travel together in their original order, instead of
        flattening both onto the last unconditional slide.
        """
        by_identity = dict(zip(self.order, block_ids))
        out = {}
        for identity in self.order:
            mine = set(self.seen[identity]["decks"])
            walk = self.seen[identity].get("after")
            guard = set()
            while walk is not None and walk not in guard:
                guard.add(walk)
                if set(self.seen[walk]["decks"]) >= mine:
                    out[by_identity[identity]] = by_identity[walk]
                    break
                walk = self.seen[walk].get("after")
        return out


# ---------------------------------------------------------------- tokenising
# A generated deck has its values already substituted: the cover says
# "Example Corporation", not "{{ClientName}}". Merging those decks therefore
# produces a snapshot of one client, not a template. The mechanics live in
# engine/tokenise.py so the same repair can be run later on a library that
# already exists — which is what you want once PowerPoint has agreed to open
# one. Here we only walk the merged parts.
from engine.tokenise import (bindings_for, chosen_formats,  # noqa: E402
                             guess_token_map, replacements as _replacements,
                             tokenise_part)


def tokenise(merger, payloads, token_map=None):
    """Put placeholders back into every merged slide. -> report dict."""
    totals, per_field, used_map = {}, {}, dict(token_map or {})
    fmt_counts = {}
    for identity in merger.order:
        rec = merger.seen[identity]
        payload = payloads.get(rec["first_deck"])
        if not payload:
            continue
        if not used_map:
            used_map = guess_token_map(payload)
        reps = _replacements(payload, used_map)
        part = rec["part"]
        xml = merger.out.parts[part].decode("utf-8", "ignore")
        new_xml, hits, formats = tokenise_part(xml, reps)
        if hits:
            merger.out.parts[part] = new_xml.encode("utf-8")
            # the title is what blocks.json records; take it from the text as
            # it will actually be, not as it was before substitution
            rec["title"] = slide_title(new_xml)
        for literal, n in hits.items():
            totals[literal] = totals.get(literal, 0) + n
        for field, seen_fmts in formats.items():
            for fmt, n in seen_fmts.items():
                fmt_counts.setdefault(field, {})
                fmt_counts[field][fmt] = fmt_counts[field].get(fmt, 0) + n
    for field, name in used_map.items():
        per_field[name] = field
    return {"replacements": totals, "map": per_field, "fields": used_map,
            "formats": chosen_formats(fmt_counts)}


def _placeholder_bindings(token_report):
    """The placeholders map for rules.json, including the date rendering the
    source decks actually used."""
    if not token_report:
        return {}
    return bindings_for(token_report.get("fields", {}),
                        token_report.get("formats", {}))


# ---------------------------------------------------------------- rules
def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, "%s.%s" % (prefix, k) if prefix else str(k)))
    else:
        out[prefix] = obj
    return out


def propose_rules(block_ids, blocks, payloads):
    """Work out *why* each slide appeared, from which decks contained it.

    The payloads are OFAT — one field changed at a time — so a slide that shows
    up in exactly the decks where `Peer_review` is true is almost certainly
    controlled by that answer. We look for a single (field, value) pair whose
    decks match the slide's decks exactly, and propose it.

    This is a **proposal, not a conclusion**. Anything unexplained is left in
    the baseline and flagged, because a wrong rule that looks confident is worse
    than an obvious gap. The author confirms it (workflow step 3).
    """
    all_decks = set()
    for _p, _t, decks, _h in blocks:
        all_decks.update(decks)
    all_decks &= set(payloads)
    if not all_decks:
        return {}, []

    # (field, value) -> the decks whose payload has it
    by_value = {}
    for deck in all_decks:
        for field, value in flatten(payloads[deck]).items():
            by_value.setdefault((field, _norm(value)), set()).add(deck)

    proposals, notes = {}, []
    for bid, (_part, _title, decks, _how) in zip(block_ids, blocks):
        present = set(decks) & all_decks
        if present == all_decks:
            continue                                  # in every deck: baseline
        matches = sorted((f, v) for (f, v), d in by_value.items() if d == present)
        # a field that never varies explains nothing, even if the sets match
        matches = [(f, v) for f, v in matches
                   if len({_norm(x) for x in
                           (flatten(p).get(f) for p in payloads.values())}) > 1]
        if len(matches) == 1:
            field, value = matches[0]
            proposals[bid] = {"field": field, "value": value,
                              "decks": sorted(present)}
        elif matches:
            proposals[bid] = {"field": matches[0][0], "value": matches[0][1],
                              "decks": sorted(present),
                              "ambiguous": ["%s=%s" % m for m in matches[:6]]}
            notes.append("%s: %d answers explain it equally well (%s) — picked "
                         "the first; confirm which is right"
                         % (bid, len(matches),
                            ", ".join("%s=%s" % m for m in matches[:3])))
        else:
            notes.append("%s: appears in %d of %d decks but no single answer "
                         "explains it — left in the baseline, needs a human"
                         % (bid, len(present), len(all_decks)))
    return proposals, notes


def _norm(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return re.sub(r"\s+", " ", str(value)).strip()


def load_payloads(folder, deck_labels):
    """Pair each deck with its payload by file name (`01_x.pptx` <- `01_x.json`,
    or a shared numeric prefix)."""
    if not folder or not os.path.isdir(folder):
        return {}, []
    files = {os.path.splitext(f)[0]: os.path.join(folder, f)
             for f in os.listdir(folder) if f.endswith(".json")}
    by_prefix = {}
    for stem in files:
        m = re.match(r'(\d+)', stem)
        if m:
            by_prefix.setdefault(m.group(1), stem)

    paired, unpaired = {}, []
    for label in deck_labels:
        stem = None
        if label in files:
            stem = label
        else:
            m = re.match(r'(\d+)', label)
            if m and m.group(1) in by_prefix:
                stem = by_prefix[m.group(1)]
        if stem is None:
            unpaired.append(label)
            continue
        with open(files[stem], encoding="utf-8") as fh:
            paired[label] = json.load(fh)
    return paired, unpaired


# ---------------------------------------------------------------- probe
def probe(decks, limit=2):
    """Report how slides would be identified, without merging anything.

    Cheap to run and answers the only question that matters before a merge: are
    the creationIds there? If they are not, every deck's copy of a slide looks
    different (its client name differs) and the merge de-duplicates nothing.
    """
    print("Probing %d deck(s)" % min(len(decks), limit))
    overall, notes = {}, {}
    for path in decks[:limit]:
        label = os.path.splitext(os.path.basename(path))[0]
        try:
            with Library(path) as lib:
                counts = {}
                for part in lib.slide_parts:
                    _key, how = slide_identity(lib.read_bytes(part), notes)
                    counts[how] = counts.get(how, 0) + 1
                    overall[how] = overall.get(how, 0) + 1
                print("  %-30s %3d slides: %s"
                      % (label[:30], len(lib.slide_parts),
                         ", ".join("%s=%d" % kv for kv in sorted(counts.items()))))
        except Exception as exc:
            print("  %-30s could not be read: %s: %s"
                  % (label[:30], type(exc).__name__, exc))

    print("")
    for reason in notes.values():
        print("  ! %s" % reason)
    if overall.get("creationId"):
        print("  creationIds found - slides will match across decks reliably.")
        return 0
    print("  ! NO creationIds found in these decks.")
    print("    Every deck's copy of a slide differs by its client name, so")
    print("    matching falls back to %s and the merge will de-duplicate"
          % ", ".join(sorted(overall)))
    print("    little or nothing. Merging anyway produces one block per slide")
    print("    per deck - a huge library that is not a template.")
    return 1


# ---------------------------------------------------------------- cli
def collect_decks(inputs):
    decks = []
    for item in inputs:
        if os.path.isdir(item):
            decks.extend(sorted(glob.glob(os.path.join(item, "*.pptx"))))
        else:
            decks.extend(sorted(glob.glob(item)) or [item])
    seen, out = set(), []
    for d in decks:
        real = os.path.abspath(d)
        if real not in seen and not os.path.basename(d).startswith("~$"):
            seen.add(real)
            out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decks", nargs="+", help="deck files, globs, or a folder")
    ap.add_argument("--name", default="firm", help="template name to create")
    ap.add_argument("--templates", default=os.path.join(HERE, "templates"))
    ap.add_argument("--payloads", help="folder of payload .json, to propose rules")
    ap.add_argument("--tokenise", "--tokenize", dest="tokenise", action="store_true",
                    help="put {{placeholders}} back where the payload's values "
                         "were substituted (needs --payloads)")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--allow-weak-identity", action="store_true",
                    help="merge even when slides cannot be matched across decks "
                         "(the result will barely de-duplicate)")
    ap.add_argument("--probe", action="store_true",
                    help="report how slides would be identified, then stop")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    decks = collect_decks(args.decks)
    if args.probe:
        return probe(decks)
    if not decks:
        ap.error("no .pptx found in %s" % ", ".join(args.decks))

    folder = os.path.join(args.templates, args.name)
    if os.path.exists(folder) and not args.overwrite:
        ap.error("template %r already exists at %s — pass --overwrite"
                 % (args.name, folder))
    os.makedirs(folder, exist_ok=True)

    print("Merging %d deck(s)" % len(decks))
    merger = Merger(verbose=args.verbose)
    labels = []
    for n, path in enumerate(decks, 1):
        label = os.path.splitext(os.path.basename(path))[0]
        labels.append(label)
        try:
            touched = merger.add_deck(path, label)
        except Exception as exc:
            print("  ! %s skipped: %s: %s" % (label, type(exc).__name__, exc))
            continue
        added = sum(1 for _i, is_new, _t in touched if is_new)
        if args.verbose or added:
            print("  [%d/%d] %-34s %3d slides, %2d new"
                  % (n, len(decks), label[:34], len(touched), added))

    payloads, unpaired = load_payloads(args.payloads, labels)

    token_report = None
    if args.tokenise:
        if not payloads:
            ap.error("--tokenise needs --payloads: the payload is what tells us "
                     "which literals were substituted values")
        token_report = tokenise(merger, payloads)

    # Stop before writing 80MB of near-duplicates. If nothing matched across
    # decks, the merge has not merged anything and the output is not a template.
    weak = not merger.id_methods.get("creationId")
    collapsed = sum(len(d) for _p, _t, d, _h in merger.blocks())
    if weak and collapsed and len(merger.order) > 0.8 * collapsed             and not args.allow_weak_identity:
        print("")
        print("STOPPING: %d slides across the decks produced %d blocks - almost"
              % (collapsed, len(merger.order)))
        print("nothing matched, so this is not a merge. Slides were identified by:")
        for how, n in sorted(merger.id_methods.items(), key=lambda kv: -kv[1]):
            print("    %-16s %d" % (how, n))
        for reason in merger.id_notes.values():
            print("    ! %s" % reason)
        print("")
        print("Run `--probe` on a couple of decks to see why. Pass")
        print("--allow-weak-identity to merge anyway.")
        shutil.rmtree(folder, ignore_errors=True)
        return 2

    out_pptx = os.path.join(folder, "library.pptx")
    merger.write(out_pptx)
    blocks = merger.blocks()

    # block ids from slide titles, disambiguated
    ids, used = [], {}
    for _part, title, _decks, _how in blocks:
        bid = slugify(title, fallback="") or "slide"
        if bid in used:
            used[bid] += 1
            bid = "%s_%d" % (bid, used[bid])
        else:
            used[bid] = 1
        ids.append(bid)

    with open(os.path.join(folder, "blocks.json"), "w", encoding="utf-8") as fh:
        json.dump({"_comment": "Slide -> block id, derived from slide titles. "
                               "Rename freely; rules.json refers to these ids.",
                   "blocks": {os.path.basename(p): {"id": b, "title": t}
                              for b, (p, t, _d, _h) in zip(ids, blocks)}},
                  fh, indent=2, ensure_ascii=False)

    # worked out from each deck's own slide order, and recorded below
    anchors = merger.anchors(ids)

    # The anchor is worked out from each deck's own slide order, which the
    # library cannot reproduce: a block introduced by a later deck sits at the
    # end of the library regardless of where it sat in its deck. Recording it
    # here is what lets the rules be rebuilt later without re-merging.
    provenance = {b: {"title": t, "slide": os.path.basename(p),
                      "identified_by": h, "deck_count": len(d), "decks": d,
                      "after": anchors.get(b)}
                  for b, (p, t, d, h) in zip(ids, blocks)}
    with open(os.path.join(folder, "provenance.json"), "w", encoding="utf-8") as fh:
        json.dump({"_comment": "Which generated decks each slide came from. "
                               "This is the evidence behind rules.json.",
                   "deck_count": len(labels), "blocks": provenance},
                  fh, indent=2, ensure_ascii=False)

    proposals, notes = ({}, [])
    if payloads:
        proposals, notes = propose_rules(ids, blocks, payloads)

    baseline = [b for b in ids if b not in proposals]
    rule_blocks, unanchored = {}, []
    for bid, info in proposals.items():
        rule_blocks[bid] = {
            "slides": [bid],
            "when": {"field": info["field"], "eq": info["value"]},
            "_evidence": "in %d deck(s): %s" % (len(info["decks"]),
                                                ", ".join(info["decks"][:4])),
        }
        # the anchor may itself be conditional — `anchors()` only offers one
        # that is present whenever this block is, and the rules engine places
        # them in dependency order
        anchor = anchors.get(bid)
        if anchor and (anchor in baseline or anchor in proposals):
            rule_blocks[bid]["insert_after"] = anchor
        else:
            unanchored.append(bid)
        if info.get("ambiguous"):
            rule_blocks[bid]["_confirm"] = ("equally explained by: %s"
                                            % ", ".join(info["ambiguous"]))
    with open(os.path.join(folder, "rules.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "name": args.name,
            "library": "library.pptx",
            "_comment": ("Baseline = slides in every deck. Blocks with a `when` "
                         "are PROPOSED from which decks contained them — confirm "
                         "each before trusting it. Evidence: provenance.json."),
            "baseline": baseline,
            "blocks": rule_blocks,
            "placeholders": _placeholder_bindings(token_report),
        }, fh, indent=2, ensure_ascii=False)

    # A payload IS a set of answers, so the merge can leave one behind for
    # `check` to build a test deck from. Without it the check has to skip the
    # one step that proves the template actually works.
    if payloads:
        first = sorted(payloads)[0]
        with open(os.path.join(folder, "answers.sample.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payloads[first], fh, indent=2, ensure_ascii=False)

    with open(os.path.join(folder, "template.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": args.name,
                   "description": "Master library merged from %d generated decks"
                                  % len(labels),
                   "library": "library.pptx"}, fh, indent=2)

    # ---- report
    total_slides = sum(len(d) for _p, _t, d, _h in blocks)
    print("\nMaster library: %s" % out_pptx)
    print("  %d unique slides from %d deck(s) (%d slide instances collapsed)"
          % (len(blocks), len(labels), total_slides))
    for how, n in sorted(merger.id_methods.items(), key=lambda kv: -kv[1]):
        print("      identified by %-16s %d" % (how, n))
    if "creationId" not in merger.id_methods and blocks:
        print("      ! no creationIds found — slides were matched on structure "
              "and text,\n        so slides differing only by client name may "
              "have been counted twice.")
    print("  %d baseline (in every deck), %d conditional (proposed)"
          % (len(baseline), len(proposals)))
    print("  ! block ids come from slide titles, which in a generated deck may")
    print("    contain client text - review: python build.py inspect %s" % args.name)
    if unpaired:
        print("  ! no payload matched for %d deck(s): %s"
              % (len(unpaired), ", ".join(unpaired[:5])))

    if token_report is not None:
        hits = token_report["replacements"]
        print("")
        print("  Placeholders restored:")
        for literal, n in sorted(hits.items(), key=lambda kv: -kv[1]):
            print("      %-34s %3d occurrence(s)" % ('"%s"' % literal[:32], n))
        if not hits:
            print("      none - the payload's values do not appear in the deck "
                  "text as written")
        print("      Check these: a value can be a real answer in one place and")
        print("      ordinary wording in another (v1 hit this with \"New York\").")
        print("      Review with: python build.py preview %s" % args.name)
    elif payloads:
        print("")
        print("  ! No placeholders in the library: these are GENERATED decks, so")
        print("    values like the client name are already substituted. The library")
        print("    is a snapshot of one client until you re-run with --tokenise.")
    if proposals:
        print("  %d of %d conditional block(s) placed with insert_after"
              % (len(proposals) - len(unanchored), len(proposals)))
    for bid in unanchored:
        print("  ? %s has no stable slide before it - it will be appended; "
              "set insert_after by hand" % bid)
    for note in notes:
        print("  ? %s" % note)

    validator = _load_validator()
    if validator:
        issues = validator.validate(out_pptx)
        if issues:
            print("\n  FAILED validation - %d issue(s):" % len(issues))
            for kind, where, detail in issues[:15]:
                print("      [%s] %s: %s" % (kind, where, detail))
            return 1
        print("  validated OK - no repair dialog expected")

    print("\nNext:")
    print("  python build.py inspect %s      # check the block ids read well" % args.name)
    print("  python build.py preview %s      # look at every slide" % args.name)
    print("  then confirm the proposed `when` rules in %s"
          % os.path.join(folder, "rules.json"))
    return 0


def _load_validator():
    """Vendored with the engine, so a standalone deployment still validates."""
    from engine import validate as _validate
    return _validate


if __name__ == "__main__":
    sys.exit(main())
