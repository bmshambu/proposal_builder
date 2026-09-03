#!/usr/bin/env python3
"""
diff_decks.py  -  compare an ORIGINAL (Templafy) deck against a RECONSTRUCTED one.

This is the validation step: since both decks derive from the same 265-slide
master, a correct reconstruction should keep the SAME slides (validates the
selection rules) and contain the SAME text (validates token injection).

It aligns slides between the two decks by shape creationId GUIDs (token-invariant),
then reports:
  * selection: slides present in one deck but not the other,
  * order: whether matched slides appear in the same sequence,
  * text: word-level differences on matched slides (residual diffs are usually
          dynamic tokens not yet in token_map),
  * images: per-slide image-count differences.

Standard library only, read-only.

CLI
    python diff_decks.py --original data/decks/01_audit.pptx \
                        --rebuilt  data/output/01_audit_rebuilt.pptx
"""

import argparse
import difflib
import json

import pptx_forensics as pf

MATCH_THRESHOLD = 0.34


def _segments(original_text, rebuilt_text):
    """Concrete word-level changes turning the REBUILT text into the ORIGINAL.
    Returns [{was, should_be}] where `was` is the rebuilt phrase (wrong) and
    `should_be` is the original/Templafy phrase (correct) — ready to become a
    find->replace fixup. Skips pure insertions (nothing to 'find')."""
    a, b = original_text.split(), rebuilt_text.split()   # a=original, b=rebuilt
    segs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "replace":
            segs.append({"was": " ".join(b[i1:i2]), "should_be": " ".join(a[j1:j2])})
        elif tag == "delete":                            # extra words in rebuild
            segs.append({"was": " ".join(b[i1:i2]), "should_be": ""})
    return [s for s in segs if s["was"].strip()][:5]


def _align(a_slides, b_slides):
    """Greedy best-match a_slides -> b_slides by similarity. Returns
    (pairs, only_a, only_b) where pairs is [(a, b, method, score)]."""
    used_b = set()
    pairs, only_a = [], []
    for a in a_slides:
        best, bmethod, bscore = None, None, 0.0
        for b in b_slides:
            if b["index"] in used_b:
                continue
            method, score = pf.slide_similarity(a, b)
            if score > bscore:
                best, bmethod, bscore = b, method, score
        if best and bscore >= MATCH_THRESHOLD:
            used_b.add(best["index"])
            pairs.append((a, best, bmethod, round(bscore, 3)))
        else:
            only_a.append(a)
    only_b = [b for b in b_slides if b["index"] not in used_b]
    return pairs, only_a, only_b


def diff(original_path, rebuilt_path):
    o = pf.load_deck(original_path)
    r = pf.load_deck(rebuilt_path)
    pairs, only_original, only_rebuilt = _align(o["slides"], r["slides"])

    # order check: matched rebuilt indices should be increasing in original order
    r_seq = [b["index"] for (_a, b, _m, _s) in pairs]
    order_ok = all(r_seq[i] <= r_seq[i + 1] for i in range(len(r_seq) - 1))

    text_mismatches, image_mismatches, creationid_matches, structural_matches = [], [], 0, 0
    for a, b, method, score in pairs:
        if method == "creationId":
            creationid_matches += 1
        elif method == "structural":
            structural_matches += 1
        aw, bw = pf.word_set(a["text"]), pf.word_set(b["text"])
        if aw != bw:
            text_mismatches.append({
                "original_index": a["index"], "rebuilt_index": b["index"],
                "only_in_original": sorted(aw - bw)[:25],
                "only_in_rebuilt": sorted(bw - aw)[:25],
                "segments": _segments(a["text"], b["text"]),
            })
        if len(a["images"]) != len(b["images"]):
            image_mismatches.append({
                "original_index": a["index"], "rebuilt_index": b["index"],
                "original_images": len(a["images"]),
                "rebuilt_images": len(b["images"]),
            })

    if only_original or only_rebuilt:
        verdict = "SELECTION DIFFERS"
    elif text_mismatches:
        verdict = "SELECTION OK, TEXT DIFFERS (likely tokens)"
    else:
        verdict = "MATCH"

    return {
        "original": o["name"], "rebuilt": r["name"],
        "verdict": verdict,
        "counts": {
            "original_slides": o["slide_count"],
            "rebuilt_slides": r["slide_count"],
            "matched": len(pairs),
            "via_creationId": creationid_matches,
            "via_structural": structural_matches,
            "only_in_original": len(only_original),
            "only_in_rebuilt": len(only_rebuilt),
            "text_mismatches": len(text_mismatches),
            "image_mismatches": len(image_mismatches),
        },
        "order_ok": order_ok,
        "only_in_original": [{"index": a["index"], "preview": a["text"][:70]} for a in only_original],
        "only_in_rebuilt": [{"index": b["index"], "preview": b["text"][:70]} for b in only_rebuilt],
        "text_mismatches": text_mismatches,
        "image_mismatches": image_mismatches,
    }


def main():
    ap = argparse.ArgumentParser(description="Diff an original vs reconstructed deck.")
    ap.add_argument("--original", required=True)
    ap.add_argument("--rebuilt", required=True)
    ap.add_argument("--json", action="store_true", help="print full JSON")
    args = ap.parse_args()
    res = diff(args.original, args.rebuilt)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print("Verdict:", res["verdict"])
        print("Counts:", json.dumps(res["counts"]))
        print("Order preserved:", res["order_ok"])
        if res["only_in_original"]:
            print("Missing from rebuild:", [x["index"] for x in res["only_in_original"]])
        if res["only_in_rebuilt"]:
            print("Extra in rebuild:", [x["index"] for x in res["only_in_rebuilt"]])
        print("Text mismatches:", res["counts"]["text_mismatches"])


if __name__ == "__main__":
    main()
