#!/usr/bin/env python3
"""
pptx_forensics.py  -  shared, read-only OOXML parsing helpers.

Used by inspect_pptx.py and map_decks.py. Standard library only (Python 3.8+),
no third-party dependencies, no network calls, never modifies the .pptx.

The important idea: when Templafy assembles a deck it COPIES slides out of the
big master template. Copied shapes keep their Microsoft "creationId" GUIDs
(a16:creationId) and slides keep their p14:creationId. Those survive text edits
(injecting a client name does NOT change them), so they are the most reliable
way to match a generated slide back to the master slide it came from.
"""

import os
import re
import zipfile
import xml.etree.ElementTree as ET


# ---------------------------------------------------------------- namespace utils
def local(tag):
    """'{ns}sld' -> 'sld'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def iter_local(elem, name):
    """Descendants whose local-name == name (namespace-agnostic)."""
    for e in elem.iter():
        if local(e.tag) == name:
            yield e


def attr_local(elem, name):
    """Attribute value by local-name (namespace-agnostic)."""
    for k, v in elem.attrib.items():
        if local(k) == name:
            return v
    return None


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- shape parsing
SHAPE_TAGS = {"sp", "pic", "graphicFrame", "cxnSp", "grpSp"}


def _creation_ids(elem):
    """All creationId GUIDs anywhere under elem (shape-level, a16:creationId).

    Value lives in the 'id' attribute, e.g. id="{7C2...}". We also accept
    p14:creationId's numeric 'val' as a weaker signal.
    """
    ids = set()
    for e in iter_local(elem, "creationId"):
        val = attr_local(e, "id") or attr_local(e, "val")
        if val:
            ids.add(val.strip("{}").lower())
    return ids


def _texts(elem):
    """All a:t text runs under elem, in document order."""
    return [(t.text or "") for t in iter_local(elem, "t")]


def _geometry(sp):
    """Shape position/size (x, y, w, h) in points, token-invariant. Filling a
    placeholder with text does NOT move the shape, so geometry is a stable
    fingerprint for matching a filled slide back to its placeholder master."""
    off = next((e for e in iter_local(sp, "off") if attr_local(e, "x") is not None), None)
    ext = next((e for e in iter_local(sp, "ext") if attr_local(e, "cx") is not None), None)
    if off is None or ext is None:
        return None
    try:
        return (int(attr_local(off, "x")) // 12700, int(attr_local(off, "y")) // 12700,
                int(attr_local(ext, "cx")) // 12700, int(attr_local(ext, "cy")) // 12700)
    except (TypeError, ValueError):
        return None


def parse_shape(sp):
    """One top-level shape -> dict."""
    name = descr = None
    ph_type = ph_idx = None
    for cnv in iter_local(sp, "cNvPr"):
        name = attr_local(cnv, "name")
        descr = attr_local(cnv, "descr")
        break
    for ph in iter_local(sp, "ph"):
        ph_type = attr_local(ph, "type") or "body"
        ph_idx = attr_local(ph, "idx")
        break
    texts = [t for t in _texts(sp) if t]
    return {
        "kind": local(sp.tag),
        "name": name,
        "descr": descr,
        "ph_type": ph_type,
        "ph_idx": ph_idx,
        "geom": _geometry(sp),
        "creation_ids": sorted(_creation_ids(sp)),
        "text": " ".join(texts).strip(),
        # smart-field marker: binding JSON is often stashed in name/descr
        "is_smartfield": bool(
            (name and ("{" in name or "templafy" in name.lower())) or
            (descr and ("{" in descr or "templafy" in descr.lower()
                        or "binding" in descr.lower()))
        ),
    }


def parse_slide_xml(xml_bytes):
    """Slide part XML -> structured dict (shapes, text, creationIds)."""
    root = ET.fromstring(xml_bytes)
    # slide-level creationId (p14)
    slide_cid = None
    for e in iter_local(root, "creationId"):
        # the first creationId directly under the slide's extLst is slide-level;
        # good enough to grab the first val-bearing one
        v = attr_local(e, "val")
        if v:
            slide_cid = v
            break

    shapes = []
    sptrees = list(iter_local(root, "spTree"))
    if sptrees:
        for child in sptrees[0]:
            if local(child.tag) in SHAPE_TAGS:
                shapes.append(parse_shape(child))

    all_cids = set()
    for s in shapes:
        all_cids.update(s["creation_ids"])
    all_text = " ".join(s["text"] for s in shapes if s["text"]).strip()

    # token-invariant structural signature: (shape kind, placeholder, geometry)
    geom_sig = frozenset(
        (s["kind"], s["ph_type"] or "", s["geom"])
        for s in shapes if s["geom"] is not None
    )

    return {
        "slide_creation_id": slide_cid,
        "shapes": shapes,
        "shape_creation_ids": sorted(all_cids),
        "geom_sig": geom_sig,
        "text": all_text,
        "placeholders": [
            (s["ph_type"] + ("#" + s["ph_idx"] if s["ph_idx"] else ""))
            for s in shapes if s["ph_type"]
        ],
        "smartfields": [s for s in shapes if s["is_smartfield"]],
    }


# ---------------------------------------------------------------- deck loading
def _slide_order(zf, names):
    """Return slide part names in true presentation order (via rels)."""
    prs = "ppt/presentation.xml"
    rels = "ppt/_rels/presentation.xml.rels"
    if prs not in names or rels not in names:
        # fall back to numeric order
        return sorted([n for n in names if re.search(r"ppt/slides/slide\d+\.xml$", n)],
                      key=natural_key)
    rid_to_target = {}
    for m in re.finditer(r'Id="([^"]+)"\s+Type="[^"]*"\s+Target="([^"]+)"',
                         zf.read(rels).decode("utf-8", "ignore")):
        rid_to_target[m.group(1)] = m.group(2)
    # also handle attribute order variations
    for m in re.finditer(r'Target="([^"]+)"[^>]*Id="([^"]+)"',
                         zf.read(rels).decode("utf-8", "ignore")):
        rid_to_target.setdefault(m.group(2), m.group(1))

    order = []
    prs_xml = zf.read(prs).decode("utf-8", "ignore")
    for m in re.finditer(r'<p:sldId[^>]*r:id="([^"]+)"', prs_xml):
        tgt = rid_to_target.get(m.group(1))
        if tgt:
            tgt = tgt.split("/")[-1]
            order.append("ppt/slides/" + tgt)
    if not order:
        return sorted([n for n in names if re.search(r"ppt/slides/slide\d+\.xml$", n)],
                      key=natural_key)
    return order


def _rels_targets(zf, names, slide_part):
    """(embedded_images, external_targets, layout_name) for a slide part."""
    base = os.path.basename(slide_part)
    rels = "ppt/slides/_rels/%s.rels" % base
    if rels not in names:
        return [], [], None
    raw = zf.read(rels).decode("utf-8", "ignore")
    imgs = [os.path.basename(t) for t in re.findall(r'Target="([^"]*media[^"]*)"', raw)]
    ext = re.findall(r'Target="(https?://[^"]+)"', raw)
    lay = re.search(r'Target="([^"]*slideLayout[^"]*)"', raw)
    layout = os.path.basename(lay.group(1)) if lay else None
    return imgs, ext, layout


def load_deck(path):
    """Full deck -> dict with ordered slides + custom XML + doc props.

    Slides carry index (1-based display order), part name, creationIds, text,
    placeholders, images and external links.
    """
    if not zipfile.is_zipfile(path):
        raise ValueError("Not a valid .pptx: %s" % path)
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    order = _slide_order(zf, names)

    slides = []
    for i, part in enumerate(order, start=1):
        try:
            data = parse_slide_xml(zf.read(part))
        except Exception as e:
            data = {"slide_creation_id": None, "shapes": [], "shape_creation_ids": [],
                    "geom_sig": frozenset(), "text": "", "placeholders": [],
                    "smartfields": [], "parse_error": str(e)}
        imgs, ext, layout = _rels_targets(zf, names, part)
        data["index"] = i
        data["part"] = part
        data["images"] = imgs
        data["external_links"] = ext
        data["layout"] = layout
        slides.append(data)

    customxml = {n: zf.read(n).decode("utf-8", "ignore")
                 for n in names if n.startswith("customXml/") and n.endswith(".xml")}

    zf.close()
    return {
        "path": path,
        "name": os.path.basename(path),
        "slide_count": len(slides),
        "slides": slides,
        "customxml_parts": len(customxml),
        "customxml": customxml,
    }


# ---------------------------------------------------------------- similarity
_word_re = re.compile(r"\w+", re.UNICODE)


def word_set(text):
    return set(_word_re.findall(text.lower()))


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def struct_similarity(gen, mast):
    """Layout + shape-geometry overlap. Token-invariant: filling placeholders
    with text doesn't move shapes, so a filled generated slide still matches its
    placeholder master slide here even when creationIds and text don't."""
    gg, mg = gen.get("geom_sig"), mast.get("geom_sig")
    if not gg or not mg:
        return 0.0
    j = jaccard(gg, mg)
    # small boost when the two slides use the same slide layout
    if gen.get("layout") and gen.get("layout") == mast.get("layout"):
        j = min(1.0, j + 0.1)
    return j


def slide_similarity(gen, mast):
    """Score 0..1 that generated slide `gen` originated from master `mast`.

    Tries, in order of reliability:
      1. shape creationId overlap  (exact, when preserved),
      2. layout + geometry overlap (survives placeholder filling),
      3. text word overlap         (only for static slides).
    Returns (method, score) for the best signal.
    """
    gcids, mcids = gen["shape_creation_ids"], mast["shape_creation_ids"]
    if gcids and mcids:
        j = jaccard(gcids, mcids)
        if j > 0:
            return ("creationId", j)
    s = struct_similarity(gen, mast)
    t = jaccard(word_set(gen["text"]), word_set(mast["text"]))
    return ("structural", s) if s >= t else ("text", t)
