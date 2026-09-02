#!/usr/bin/env python3
"""
reconstruct.py  -  headless deck assembler (the "Reconstruct" stage).

Rebuilds a proposal deck for a NEW payload WITHOUT Templafy, using the same
mechanism Templafy uses: start from the ~265-slide master and DELETE the slides
this payload didn't select, then inject dynamic tokens (client name, date, ...).

Because we only ever delete slides from ONE deck (never copy slides between
decks), every slide master / layout / theme / media relationship stays intact,
so branding is preserved automatically.

INPUTS
    * master.pptx           - the 265-slide superset template.
    * mapping_spec.json      - produced by map_decks.py (selection + token rules).
    * payload.json           - the new proposal's inputs (same schema Templafy uses).
    * token_map.json (opt)   - literal find -> payload-field replacements for
                               dynamic text (see below). Safe to omit for v1.

DETERMINISM
    The source deck is deterministic (confirmed: no AI component), so a given
    payload must always yield the same deck. This module is likewise pure/
    deterministic and can be diffed against the real Templafy output to validate.

Standard library only. Read-only on all inputs; writes one new .pptx.

CLI
    python reconstruct.py --master data/master/master.pptx \
        --spec data/mapping/mapping_spec.json \
        --payload data/payloads/01_audit.json \
        --out data/output/01_audit_rebuilt.pptx
"""

import argparse
import json
import os
import re
import shutil
import zipfile

import pptx_forensics as pf


# ------------------------------------------------------------ payload flattening
def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, (prefix + str(k) + ".")))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, prefix + str(i) + "."))
    else:
        if obj is not None:
            out[prefix.rstrip(".")] = obj
    return out


# ------------------------------------------------------------ selection
def resolve_selection(spec, payload_flat):
    """Return (keep_indices, warnings) for a payload given the mapping spec.

    Rules per master slide:
      always      -> keep
      conditional -> keep iff payload[field] in values
      unclear     -> keep (safer to include) + warn
      never_used  -> drop + note (never appeared in training payloads)
    """
    rules = spec.get("selection_rules", {})
    keep, warnings = [], []
    # master slide count
    n = spec.get("master", {}).get("slides") or max(
        (int(k) for k in rules), default=0)
    for i in range(1, int(n) + 1):
        r = rules.get(str(i)) or rules.get(i) or {"rule": "unclear"}
        kind = r.get("rule")
        if kind == "always":
            keep.append(i)
        elif kind == "conditional":
            field, values = r.get("field"), set(map(str, r.get("values", [])))
            val = payload_flat.get(field)
            if val is not None and str(val) in values:
                keep.append(i)
            # else drop
            if r.get("confidence") == "low":
                warnings.append("slide %d: low-confidence rule on '%s'=%s"
                                % (i, field, sorted(values)))
        elif kind == "never_used":
            warnings.append("slide %d: never used in training payloads - dropped "
                            "(may be needed for unseen field combos)" % i)
        else:  # unclear / unknown
            keep.append(i)
            warnings.append("slide %d: unclear rule - kept by default" % i)
    return keep, warnings


# ------------------------------------------------------------ presentation parse
def _read(zf, name):
    return zf.read(name).decode("utf-8", "ignore")


def parse_presentation(zf, names):
    """Return ordered list of dicts: {index, r_id, part} in display order."""
    rels = "ppt/_rels/presentation.xml.rels"
    prs = "ppt/presentation.xml"
    rid_to_target = {}
    if rels in names:
        for m in re.finditer(r'<Relationship\b[^>]*?/>', _read(zf, rels)):
            tag = m.group(0)
            rid = re.search(r'Id="([^"]+)"', tag)
            tgt = re.search(r'Target="([^"]+)"', tag)
            typ = re.search(r'Type="([^"]+)"', tag)
            if rid and tgt and typ and "slide" in typ.group(1) and "slideMaster" not in typ.group(1):
                target = tgt.group(1).split("/")[-1]
                rid_to_target[rid.group(1)] = "ppt/slides/" + target
    order = []
    if prs in names:
        for i, m in enumerate(re.finditer(r'<p:sldId\b[^>]*?r:id="([^"]+)"[^>]*/>',
                                          _read(zf, prs)), start=1):
            rid = m.group(1)
            order.append({"index": i, "r_id": rid,
                          "part": rid_to_target.get(rid)})
    return order


# ------------------------------------------------------------ token injection
def _apply_token_map(slide_xml, token_map, payload_flat):
    """Literal find/replace inside <a:t> text. token_map: {find_text: field}."""
    changed = slide_xml
    for find_text, field in token_map.items():
        val = payload_flat.get(field)
        if val is None:
            continue
        changed = changed.replace(find_text, str(val))
    return changed


# ------------------------------------------------------------ build
def build(master_path, spec, payload, out_path, token_map=None, verbose=True):
    payload_flat = {k: v for k, v in flatten(payload).items()}
    keep, warnings = resolve_selection(spec, payload_flat)
    token_map = token_map or {}

    zf = zipfile.ZipFile(master_path)
    names = zf.namelist()
    order = parse_presentation(zf, names)
    total = len(order)
    keep_set = set(keep)

    kept_parts, dropped = set(), []
    dropped_rids = []
    for s in order:
        if s["index"] in keep_set and s["part"]:
            kept_parts.add(s["part"])
        else:
            if s["part"]:
                dropped.append(s["part"])
            dropped_rids.append(s["r_id"])

    if verbose:
        print("Master slides: %d | keeping: %d | dropping: %d"
              % (total, len(kept_parts), len(dropped)))
        for w in warnings:
            print("  WARN " + w)

    # files to physically drop: dropped slide parts + their .rels
    drop_files = set(dropped)
    for part in dropped:
        base = os.path.basename(part)
        drop_files.add("ppt/slides/_rels/%s.rels" % base)

    # edited control files
    prs_xml = _read(zf, "ppt/presentation.xml")
    for rid in dropped_rids:
        prs_xml = re.sub(r'<p:sldId\b[^>]*?r:id="%s"[^>]*/>' % re.escape(rid),
                         "", prs_xml)

    rels_name = "ppt/_rels/presentation.xml.rels"
    rels_xml = _read(zf, rels_name)
    for rid in dropped_rids:
        rels_xml = re.sub(r'<Relationship\b[^>]*?Id="%s"[^>]*/>' % re.escape(rid),
                          "", rels_xml)

    ct_name = "[Content_Types].xml"
    ct_xml = _read(zf, ct_name)
    for part in dropped:
        ct_xml = re.sub(
            r'<Override\b[^>]*?PartName="/%s"[^>]*/>' % re.escape(part), "", ct_xml)

    edited = {
        "ppt/presentation.xml": prs_xml,
        rels_name: rels_xml,
        ct_name: ct_xml,
    }

    # write the new package
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tokens_applied = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
        for item in zf.infolist():
            name = item.filename
            if name in drop_files:
                continue
            if name in edited:
                out.writestr(name, edited[name])
                continue
            if name in kept_parts and token_map:
                xml = _read(zf, name)
                new_xml = _apply_token_map(xml, token_map, payload_flat)
                if new_xml != xml:
                    tokens_applied += 1
                out.writestr(name, new_xml)
                continue
            out.writestr(item, zf.read(name))
    zf.close()

    if verbose:
        print("Wrote %s (%d slides, %d slides had tokens injected)"
              % (out_path, len(kept_parts), tokens_applied))
    return {
        "out": out_path,
        "kept": sorted(keep_set),
        "dropped_count": len(dropped),
        "warnings": warnings,
        "tokens_applied": tokens_applied,
    }


# ------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="Rebuild a deck for a payload by "
                                             "deleting unselected master slides.")
    ap.add_argument("--master", required=True)
    ap.add_argument("--spec", required=True, help="mapping_spec.json from map_decks")
    ap.add_argument("--payload", required=True, help="payload .json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--token-map", help="optional token_map.json {find_text: field}")
    args = ap.parse_args()

    spec = json.load(open(args.spec, encoding="utf-8"))
    payload = json.load(open(args.payload, encoding="utf-8"))
    token_map = json.load(open(args.token_map, encoding="utf-8")) if args.token_map else {}
    res = build(args.master, spec, payload, args.out, token_map=token_map)
    print(json.dumps({k: v for k, v in res.items() if k != "warnings"}, indent=2))


if __name__ == "__main__":
    main()
