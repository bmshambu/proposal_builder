#!/usr/bin/env python3
"""
harvest.py  -  build a per-asset slide-block library from OFAT decks, and
               assemble new decks from it WITHOUT Templafy.

Confirmed model for this template:
  * The 265 master holds only the ~21-slide skeleton.
  * The real content slides are inserted by Templafy from a single library asset;
    they carry STABLE creationIds across every generated deck.
  * The OFAT decks are cleanly additive: each payload field that differs from the
    baseline contributes a precise, identifiable block of slides (add / remove).

So we harvest, per asset (data/assets/<asset>/):
  * baseline.pptx        - the all-defaults deck, used as the assembly base.
  * blocks/<field=value>/ - the slides a given field-value adds (slide XML + media).
  * manifest.json        - baseline token values + every delta (adds/removes/anchor).

Then to build a NEW payload's deck: start from baseline, apply the deltas for the
fields that differ from baseline, and swap the baseline client/date literals for
the new payload's values. Deterministic, no Templafy, branding intact (all blocks
come from decks built on the same template, so masters/layouts/theme match).

Standard library only. Read-only on inputs; writes into the asset folder + output.
"""

import json
import os
import re
import shutil
import zipfile

import pptx_forensics as pf


# ---------------------------------------------------------------- payloads
def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, prefix + str(k) + "."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, prefix + str(i) + "."))
    else:
        if obj is not None:
            out[prefix.rstrip(".")] = obj
    return out


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def varied_fields(payload_flat, base_flat):
    """Fields where payload differs from baseline -> {field: value}."""
    diffs = {}
    for k in set(payload_flat) | set(base_flat):
        pv, bv = payload_flat.get(k), base_flat.get(k)
        if pv != bv:
            diffs[k] = pv
    return diffs


def _slug(field, value):
    s = "%s=%s" % (field, value)
    return re.sub(r"[^A-Za-z0-9=._-]+", "_", s)[:120]


def _condition_slug(condition):
    """Folder-safe slug for a (possibly multi-field) delta condition."""
    parts = ["%s=%s" % (k, v) for k, v in sorted(condition.items())]
    return re.sub(r"[^A-Za-z0-9=._-]+", "_", "__".join(parts))[:150]


# ---------------------------------------------------------------- alignment
def _key(slide):
    """Cross-deck slide identity: its set of shape creationId GUIDs."""
    return frozenset(slide["shape_creation_ids"])


def align(deck, base):
    """Match deck slides to baseline by creationId. Returns
    (added, removed): added = [(deck_slide, anchor_key)] where anchor_key is the
    baseline slide's key to insert AFTER (empty frozenset = insert at start);
    removed = [baseline_key,...] present in baseline but not the deck."""
    base_keys = {_key(s): s for s in base["slides"] if s["shape_creation_ids"]}
    matched_base = set()
    added, last_anchor = [], frozenset()
    for ds in deck["slides"]:
        k = _key(ds)
        hit = base_keys.get(k)
        if hit is None and k:                      # tolerate tiny creationId drift
            best, bestj = None, 0.0
            for bk, b in base_keys.items():
                j = pf.jaccard(k, bk)
                if j > bestj:
                    best, bestj = bk, j
            if bestj >= 0.6:
                hit = base_keys[best]
                k = best
        if hit is not None:
            matched_base.add(k)
            last_anchor = k
        else:
            added.append((ds, last_anchor))
    removed = [bk for bk in base_keys if bk not in matched_base]
    return added, removed


# ---------------------------------------------------------------- harvest
def _slide_rels_name(part):
    return "ppt/slides/_rels/%s.rels" % os.path.basename(part)


def _media_targets(rels_xml):
    """[(rId, target)] for media relationships in a slide's rels."""
    out = []
    for m in re.finditer(r'<Relationship\b[^>]*?/>', rels_xml):
        tag = m.group(0)
        if "media" in tag or "/image" in tag:
            rid = re.search(r'Id="([^"]+)"', tag)
            tgt = re.search(r'Target="([^"]+)"', tag)
            if rid and tgt:
                out.append((rid.group(1), tgt.group(1)))
    return out


def build_library(baseline_pptx, baseline_payload, pairs, asset_dir):
    """pairs = [(deck_path, payload_path)] for the NON-baseline OFAT decks.
    Writes baseline.pptx, blocks/, manifest.json into asset_dir. Returns manifest."""
    asset_dir = str(asset_dir)
    blocks_root = os.path.join(asset_dir, "blocks")
    if os.path.isdir(blocks_root):
        shutil.rmtree(blocks_root)
    os.makedirs(blocks_root, exist_ok=True)
    shutil.copy(baseline_pptx, os.path.join(asset_dir, "baseline.pptx"))

    base = pf.load_deck(baseline_pptx)
    base_flat = flatten(baseline_payload)

    # baseline literal token values to substitute later
    token_baseline = {}
    for key in ("FullClientName", "ShortClientName", "DueDate"):
        if key in base_flat:
            token_baseline[key] = base_flat[key]

    deltas = []
    seen_conditions = set()
    for deck_path, payload_path in pairs:
        payload = _load_json(payload_path)
        diffs = varied_fields(flatten(payload), base_flat)
        # the delta's CONDITION = every field that differs from baseline (a sector
        # change drags sub-sector with it, so conditions can be multi-field). Skip
        # the fixed/compliance field.
        condition = {k: v for k, v in diffs.items()
                     if k not in ("I agree to comply with these policies",)}
        if not condition:
            continue
        cond_key = tuple(sorted((k, str(v)) for k, v in condition.items()))
        if cond_key in seen_conditions:      # duplicate payload for same condition
            print("skip duplicate condition from %s: %s"
                  % (os.path.basename(deck_path), condition))
            continue
        seen_conditions.add(cond_key)
        deck = pf.load_deck(deck_path)
        added, removed = align(deck, base)

        slug = _condition_slug(condition)
        adds_meta = []
        if added:
            # one block folder per delta (may hold several added slides)
            block_dir = os.path.join(blocks_root, slug)
            os.makedirs(os.path.join(block_dir, "media"), exist_ok=True)
            zf = zipfile.ZipFile(deck_path)
            names = zf.namelist()
            for ds, anchor in added:
                part = ds["part"]
                slide_xml = zf.read(part)
                rels_name = _slide_rels_name(part)
                rels_xml = zf.read(rels_name).decode("utf-8", "ignore") if rels_name in names else ""
                media = []
                for rid, tgt in _media_targets(rels_xml):
                    mp = os.path.normpath(os.path.join("ppt/slides", tgt)).replace("\\", "/")
                    if mp in names:
                        mb = os.path.basename(mp)
                        with open(os.path.join(block_dir, "media", mb), "wb") as fh:
                            fh.write(zf.read(mp))
                        media.append({"rid": rid, "file": mb})
                sf = os.path.basename(part)
                with open(os.path.join(block_dir, sf), "wb") as fh:
                    fh.write(slide_xml)
                with open(os.path.join(block_dir, sf + ".rels"), "w", encoding="utf-8") as fh:
                    fh.write(rels_xml)
                adds_meta.append({
                    "slide_file": sf,
                    "anchor_key": sorted(anchor),
                    "media": media,
                })
            zf.close()

        deltas.append({
            "condition": condition,
            "label": ", ".join("%s=%s" % (k, v) for k, v in sorted(condition.items())),
            "deck": os.path.basename(deck_path),
            "block_slug": slug if added else None,
            "adds": adds_meta,
            "removes": [sorted(r) for r in removed],
        })

    manifest = {
        "asset_dir": asset_dir,
        "baseline_deck": os.path.basename(baseline_pptx),
        "baseline_slides": base["slide_count"],
        "token_baseline": token_baseline,
        "deltas": deltas,
    }
    with open(os.path.join(asset_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def load_manifest(asset_dir):
    p = os.path.join(str(asset_dir), "manifest.json")
    return _load_json(p) if os.path.exists(p) else None


# ---------------------------------------------------------------- assemble
def _read(zf, name):
    return zf.read(name).decode("utf-8", "ignore")


def _baseline_items(zf, names):
    """Ordered [{rId, part, key}] for the baseline deck's slides."""
    rels = _read(zf, "ppt/_rels/presentation.xml.rels")
    rid_to_part = {}
    for m in re.finditer(r'<Relationship\b[^>]*?/>', rels):
        tag = m.group(0)
        if re.search(r'/slide"', tag) or ("slides/slide" in tag and "slideLayout" not in tag):
            rid = re.search(r'Id="([^"]+)"', tag)
            tgt = re.search(r'Target="([^"]+)"', tag)
            if rid and tgt:
                rid_to_part[rid.group(1)] = "ppt/slides/" + os.path.basename(tgt.group(1))
    prs = _read(zf, "ppt/presentation.xml")
    items = []
    for m in re.finditer(r'<p:sldId\b[^>]*?r:id="([^"]+)"[^>]*/>', prs):
        rid = m.group(1)
        part = rid_to_part.get(rid)
        if not part:
            continue
        try:
            data = pf.parse_slide_xml(zf.read(part))
            key = frozenset(data["shape_creation_ids"])
        except Exception:
            key = frozenset()
        items.append({"rid": rid, "part": part, "key": key})
    return items, prs, rels


def _applicable(manifest, payload_flat):
    """Deltas whose FULL condition holds for this payload (every field=value in
    the condition must match)."""
    out = []
    for d in manifest["deltas"]:
        cond = d.get("condition") or {}
        if cond and all(str(payload_flat.get(f)) == str(v) for f, v in cond.items()):
            out.append(d)
    return out


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _date_variants(yyyymmdd):
    """Common human formats a YYYYMMDD date might be rendered as in a deck."""
    s = re.sub(r"\D", "", str(yyyymmdd))
    if len(s) < 8:
        return {}
    y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return {}
    mon, ab = _MONTHS[m - 1], _MONTHS[m - 1][:3]
    return {
        "long_comma": "%s %d, %d" % (mon, d, y),      # November 30, 2026
        "day_month":  "%d %s %d" % (d, mon, y),       # 30 November 2026
        "abbr_comma": "%s %d, %d" % (ab, d, y),        # Nov 30, 2026
        "day_abbr":   "%d %s %d" % (d, ab, y),         # 30 Nov 2026
        "us_slash":   "%02d/%02d/%d" % (m, d, y),      # 11/30/2026
        "eu_slash":   "%02d/%02d/%d" % (d, m, y),      # 30/11/2026
        "iso":        "%d-%02d-%02d" % (y, m, d),      # 2026-11-30
        "raw":        s,                                # 20261130
    }


def _token_replacements(manifest, payload_flat, baseline_text=""):
    """[(find, replace)] mapping baseline literals -> new values. Client/short
    names by literal; dates by whichever human format actually appears in the
    baseline deck text."""
    reps = []
    tb = manifest.get("token_baseline", {})
    for key in ("FullClientName", "ShortClientName"):
        base_val, new_val = tb.get(key), payload_flat.get(key)
        if base_val and new_val is not None and str(new_val) != str(base_val):
            reps.append((str(base_val), str(new_val)))
    # date: detect the format present in the baseline deck, map to the new date
    bd, nd = tb.get("DueDate"), payload_flat.get("DueDate")
    if bd and nd and str(bd) != str(nd):
        bvar, nvar = _date_variants(bd), _date_variants(nd)
        for k, bstr in bvar.items():
            if bstr and k in nvar and bstr in baseline_text:
                reps.append((bstr, nvar[k]))
    reps.sort(key=lambda r: -len(r[0]))          # replace longer strings first
    return reps


def _xml_unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))


def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def overrides_path(asset_dir):
    return os.path.join(str(asset_dir), "overrides.json")


def load_overrides(asset_dir):
    p = overrides_path(asset_dir)
    return _load_json(p) if os.path.exists(p) else []


def save_overrides(asset_dir, rules):
    with open(overrides_path(asset_dir), "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2)


def _override_reps(asset_dir, payload_flat):
    """Manual (human-in-the-loop) fixups -> [(find, replace)].
      kind 'token': replace `find` with the payload's value for `field` (always).
      kind 'swap' : when payload[when_field]==when_value, replace `find`->`replace_with`.
    """
    reps = []
    for r in load_overrides(asset_dir):
        find = r.get("find")
        if not find:
            continue
        if r.get("kind") == "token":
            val = payload_flat.get(r.get("field"))
            if val is not None:
                reps.append((find, str(val)))
        elif r.get("kind") == "swap":
            if str(payload_flat.get(r.get("when_field"))) == str(r.get("when_value")):
                reps.append((find, r.get("replace_with", "")))
    return reps


def _apply_tokens(xml, reps):
    """Replace token strings INSIDE each <a:t> text run individually. This is
    strictly non-structural — it never rewrites paragraphs, empties runs, or
    touches anything outside a run's text — so it cannot corrupt a slide or alter
    creationIds. Handles the common case where Templafy inserts a value (client
    name, date) as a single run. (Values split across multiple runs are left as a
    text diff rather than risking structural damage.)"""
    if not reps:
        return xml
    reps = sorted(reps, key=lambda r: -len(r[0]))   # longest match first

    def repl(m):
        raw = _xml_unescape(m.group(2))
        new = raw
        for find, rep in reps:
            if find in new:
                new = new.replace(find, rep)
        if new == raw:
            return m.group(0)
        return m.group(1) + _xml_escape(new) + m.group(3)

    return re.sub(r'(<a:t[^>]*>)(.*?)(</a:t>)', repl, xml, flags=re.DOTALL)


def assemble(asset_dir, payload, out_path, verbose=False):
    asset_dir = str(asset_dir)
    manifest = load_manifest(asset_dir)
    if not manifest:
        raise RuntimeError("No manifest — build the library first.")
    payload_flat = flatten(payload)
    baseline_pptx = os.path.join(asset_dir, "baseline.pptx")

    deltas = _applicable(manifest, payload_flat)
    removes = set(frozenset(r) for d in deltas for r in d["removes"])
    base_deck = pf.load_deck(baseline_pptx)
    baseline_text = " ".join(s["text"] for s in base_deck["slides"])
    reps = _token_replacements(manifest, payload_flat, baseline_text)
    reps += _override_reps(asset_dir, payload_flat)   # manual human-in-the-loop fixups

    zf = zipfile.ZipFile(baseline_pptx)
    names = zf.namelist()
    items, prs_xml, rels_xml = _baseline_items(zf, names)

    # collect adds: (anchor_key, slide_xml, rels_xml, [(new_media_part, bytes)], mediamap)
    max_slide_n = max([int(re.search(r'slide(\d+)\.xml', it["part"]).group(1))
                       for it in items] + [0])
    max_rid = max([int(re.search(r'\d+', r).group()) for r in
                   re.findall(r'Id="(rId\d+)"', rels_xml)] + [0])
    add_counter = 0
    prepared_adds = {}     # anchor_key(frozenset) -> list of prepared add dicts
    new_media_parts = {}   # part -> bytes
    ct_new_slide_parts = []
    slug_seen = 0

    for d in deltas:
        if not d["adds"]:
            continue
        block_dir = os.path.join(asset_dir, "blocks", d["block_slug"])
        for a in d["adds"]:
            slide_path = os.path.join(block_dir, a["slide_file"])
            if not os.path.exists(slide_path):
                continue
            slide_xml = open(slide_path, "rb").read().decode("utf-8", "ignore")
            rels_path = slide_path + ".rels"
            add_rels = open(rels_path, encoding="utf-8").read() if os.path.exists(rels_path) else ""
            max_slide_n += 1
            add_counter += 1
            new_part = "ppt/slides/slide%d.xml" % max_slide_n
            # remap media: copy under unique names, retarget rels
            for mrec in a["media"]:
                src = os.path.join(block_dir, "media", mrec["file"])
                if not os.path.exists(src):
                    continue
                ext = os.path.splitext(mrec["file"])[1].lstrip(".") or "png"
                new_name = "hv%d_%s" % (add_counter, mrec["file"])
                new_media_parts["ppt/media/" + new_name] = open(src, "rb").read()
                add_rels = add_rels.replace('Target="../media/%s"' % mrec["file"],
                                            'Target="../media/%s"' % new_name)
                add_rels = add_rels.replace('Target="/ppt/media/%s"' % mrec["file"],
                                            'Target="../media/%s"' % new_name)
            # keep only layout + media rels (drop notes/comments referencing uncopied parts)
            kept_rels = [m.group(0) for m in re.finditer(r'<Relationship\b[^>]*?/>', add_rels)
                         if ("slideLayout" in m.group(0) or "media" in m.group(0)
                             or "/image" in m.group(0))]
            add_rels_final = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                              '<Relationships xmlns="http://schemas.openxmlformats.org/'
                              'package/2006/relationships">' + "".join(kept_rels)
                              + "</Relationships>")
            prepared_adds.setdefault(frozenset(a["anchor_key"]), []).append({
                "part": new_part,
                "slide_xml": _apply_tokens(slide_xml, reps),
                "rels_xml": add_rels_final,
            })
            ct_new_slide_parts.append(new_part)

    # build final slide order (rId assigned fresh, sldId ids renumbered)
    final = []
    for a in prepared_adds.get(frozenset(), []):        # anchored to start
        final.append(("add", a))
    for it in items:
        if it["key"] not in removes:
            final.append(("keep", it))
        for a in prepared_adds.get(it["key"], []):
            final.append(("add", a))

    # assign rIds + sldIds
    sld_entries, slide_rels_entries = [], []
    rid_n = max_rid
    for i, (kind, obj) in enumerate(final):
        if kind == "keep":
            rid = obj["rid"]
        else:
            rid_n += 1
            rid = "rId%d" % rid_n
            obj["rid"] = rid
            slide_rels_entries.append(
                '<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/slide" Target="slides/%s"/>'
                % (rid, os.path.basename(obj["part"])))
        sld_entries.append('<p:sldId id="%d" r:id="%s"/>' % (256 + i, rid))

    # presentation.xml: swap the sldIdLst body
    new_prs = re.sub(r'<p:sldIdLst>.*?</p:sldIdLst>',
                     '<p:sldIdLst>' + "".join(sld_entries) + '</p:sldIdLst>',
                     prs_xml, flags=re.DOTALL)

    # presentation.xml.rels: drop removed slides' rels, add new ones
    kept_parts = {os.path.basename(o["part"]) for k, o in final if k == "keep"}
    def _keep_rel(tag):
        if not (re.search(r'/slide"', tag) or ("slides/slide" in tag and "slideLayout" not in tag)):
            return True  # non-slide rel: keep
        t = re.search(r'Target="([^"]+)"', tag)
        return bool(t and os.path.basename(t.group(1)) in kept_parts)
    rels_body = "".join(m.group(0) for m in re.finditer(r'<Relationship\b[^>]*?/>', rels_xml)
                        if _keep_rel(m.group(0)))
    new_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
                '2006/relationships">' + rels_body + "".join(slide_rels_entries)
                + "</Relationships>")

    # [Content_Types].xml: drop removed slide overrides, add new ones + media defaults
    ct = _read(zf, "[Content_Types].xml")
    # remove overrides for baseline slides that were dropped
    dropped = [it["part"] for it in items if it["key"] in removes]
    for part in dropped:
        ct = re.sub(r'<Override[^>]*PartName="/%s"[^>]*/>' % re.escape(part), "", ct)
    add_overrides = "".join(
        '<Override PartName="/%s" ContentType="application/vnd.openxmlformats-'
        'officedocument.presentationml.slide+xml"/>' % p for p in ct_new_slide_parts)
    # ensure media Defaults exist
    exts = {os.path.splitext(p)[1].lstrip(".").lower() for p in new_media_parts}
    default_ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "gif": "image/gif", "emf": "image/x-emf", "wmf": "image/x-wmf",
                  "svg": "image/svg+xml", "bmp": "image/bmp", "tiff": "image/tiff"}
    default_adds = ""
    for e in exts:
        if e and ('Extension="%s"' % e) not in ct:
            default_adds += '<Default Extension="%s" ContentType="%s"/>' % (
                e, default_ct.get(e, "application/octet-stream"))
    ct = ct.replace("</Types>", default_adds + add_overrides + "</Types>")

    # write output
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    add_by_part = {o["part"]: o for k, o in final if k == "add"}
    dropped_set = set(dropped)
    dropped_rels = {"ppt/slides/_rels/%s.rels" % os.path.basename(p) for p in dropped_set}
    edited = {"ppt/presentation.xml": new_prs,
              "ppt/_rels/presentation.xml.rels": new_rels,
              "[Content_Types].xml": ct}
    kept_slide_parts = {os.path.basename(o["part"]): o for k, o in final if k == "keep"}

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        for info in zf.infolist():
            name = info.filename
            if name in dropped_set or name in dropped_rels:
                continue
            if name in edited:
                out.writestr(name, edited[name]); continue
            if name.startswith("ppt/slides/slide") and name.endswith(".xml") \
                    and os.path.basename(name) in kept_slide_parts and reps:
                out.writestr(name, _apply_tokens(_read(zf, name), reps)); continue
            out.writestr(info, zf.read(name))
        # add harvested slides + their rels + media
        for part, o in add_by_part.items():
            out.writestr(part, o["slide_xml"])
            out.writestr("ppt/slides/_rels/%s.rels" % os.path.basename(part), o["rels_xml"])
        for part, data in new_media_parts.items():
            out.writestr(part, data)
    zf.close()

    kept_n = sum(1 for k, _ in final if k == "keep")
    add_n = sum(1 for k, _ in final if k == "add")
    if verbose:
        print("Assembled %s: %d kept + %d added (=%d), %d dropped, %d deltas, %d token reps"
              % (out_path, kept_n, add_n, kept_n + add_n, len(dropped), len(deltas), len(reps)))
    return {"out": out_path, "kept": kept_n, "added": add_n, "dropped": len(dropped),
            "deltas_applied": [d.get("label", "") for d in deltas],
            "tokens": len(reps), "total_slides": kept_n + add_n}
