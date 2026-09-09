"""Does our deck match the one Templafy produced from the same answers?

This is the acceptance test for the whole exercise. Everything else checks that
a deck is *valid*; this checks that it is *right* — same slides, same order, for
the same payload — against the only authority there is: the decks Templafy
actually generated.

The alignment is v1's, from `diff_decks.py`, and using it was the fix. The first
version of this module keyed a dictionary on slide identity, which quietly
collapsed every slide sharing an identity onto one block and reported
differences that were not there. Slides do not have a unique key you can look
up; they have a *similarity*, and matching them is an assignment problem:

  1. shape creationId overlap — exact, when Templafy preserved them,
  2. layout and geometry overlap — survives placeholder filling,
  3. text overlap — only meaningful for static slides,

taking the best signal available, greedily, one slide to one slide.

One category deserves its own name rather than being counted as an error.
Templafy regenerates data-driven slides (fees, partner names, RFP tables) per
deck with fresh creationIds, so they cannot be paired by identity even when
selection is perfectly correct. An unmatched slide on *both* sides at the same
position is that, not a mistake — v1 measured it at roughly 8% of a deck, and
calling it a failure would bury the differences that matter.
"""
import difflib
import glob
import os
import tempfile

from . import forensics as pf

# Below this, two slides are not the same slide. v1's value, kept deliberately:
# it was tuned against real generated decks, which is evidence this project has
# and a fresh guess would not be.
MATCH_THRESHOLD = 0.34


def align(original, rebuilt):
    """Greedy best-match, one slide to one slide.

    -> (pairs, only_original, only_rebuilt) where pairs is
    [(original, rebuilt, method, score)].
    """
    used, pairs, only_original = set(), [], []
    for a in original:
        best, method, score = None, None, 0.0
        for b in rebuilt:
            if b["index"] in used:
                continue
            how, value = pf.slide_similarity(a, b)
            if value > score:
                best, method, score = b, how, value
        if best is not None and score >= MATCH_THRESHOLD:
            used.add(best["index"])
            pairs.append((a, best, method, round(score, 3)))
        else:
            only_original.append(a)
    only_rebuilt = [b for b in rebuilt if b["index"] not in used]
    return pairs, only_original, only_rebuilt


def _text_difference(original_text, rebuilt_text):
    """The words that differ, as replacements turning ours into Templafy's."""
    a, b = original_text.split(), rebuilt_text.split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "replace":
            out.append({"ours": " ".join(b[i1:i2]),
                        "templafy": " ".join(a[j1:j2])})
        elif tag == "delete":
            out.append({"ours": " ".join(b[i1:i2]), "templafy": ""})
    return [d for d in out if d["ours"].strip()][:5]


def compare(original_path, rebuilt_path):
    """Compare one pair of decks. -> report dict."""
    original = pf.load_deck(original_path)
    rebuilt = pf.load_deck(rebuilt_path)
    pairs, only_original, only_rebuilt = align(original["slides"],
                                               rebuilt["slides"])

    sequence = [b["index"] for _a, b, _m, _s in pairs]
    order_ok = all(sequence[i] <= sequence[i + 1] for i in range(len(sequence) - 1))

    by_method, text_differs = {}, []
    for a, b, method, _score in pairs:
        by_method[method] = by_method.get(method, 0) + 1
        aw, bw = pf.word_set(a["text"]), pf.word_set(b["text"])
        if aw != bw:
            text_differs.append({
                "templafy_index": a["index"], "ours_index": b["index"],
                "only_in_templafy": sorted(aw - bw)[:20],
                "only_in_ours": sorted(bw - aw)[:20],
                "segments": _text_difference(a["text"], b["text"]),
            })

    # A slide unmatched on both sides at the same position is one Templafy
    # regenerates per deck, not a selection mistake.
    left = {s["index"]: s for s in only_original}
    right = {s["index"]: s for s in only_rebuilt}
    data_driven = sorted(set(left) & set(right))
    missing = [s for s in only_original if s["index"] not in right]
    extra = [s for s in only_rebuilt if s["index"] not in left]

    if missing or extra:
        verdict = "SELECTION DIFFERS"
    elif not order_ok:
        verdict = "ORDER DIFFERS"
    elif text_differs:
        verdict = "SELECTION OK, TEXT DIFFERS"
    elif data_driven:
        verdict = "SELECTION OK - %d data-driven slide(s)" % len(data_driven)
    else:
        verdict = "MATCH"

    return {
        "verdict": verdict, "order_ok": order_ok,
        "templafy_slides": original["slide_count"],
        "our_slides": rebuilt["slide_count"],
        "matched": len(pairs), "by_method": by_method,
        "missing": [{"index": s["index"], "preview": s["text"][:70]}
                    for s in missing],
        "extra": [{"index": s["index"], "preview": s["text"][:70]}
                  for s in extra],
        "data_driven": len(data_driven),
        "text_differs": text_differs,
        "selection_ok": not (missing or extra),
        "exact": verdict == "MATCH",
    }


def verify(template, decks_dir, payloads_dir, build_fn=None, limit=None):
    """Build a deck per payload and compare each with Templafy's."""
    import shutil

    from .assemble import build_template
    from .propose import load_payloads

    build_fn = build_fn or build_template
    decks = {os.path.splitext(os.path.basename(p))[0]: p
             for p in sorted(glob.glob(os.path.join(str(decks_dir), "*.pptx")))
             if not os.path.basename(p).startswith("~$")}
    payloads, unpaired = load_payloads(payloads_dir, sorted(decks))

    results = []
    tmp = tempfile.mkdtemp(prefix="pptgen2_verify_")
    try:
        for label in sorted(payloads)[:limit]:
            out = os.path.join(tmp, label + ".pptx")
            try:
                build_fn(template, payloads[label], out)
                row = compare(decks[label], out)
            except Exception as exc:
                row = {"verdict": "BUILD FAILED", "exact": False,
                       "selection_ok": False,
                       "error": "%s: %s" % (type(exc).__name__, exc)}
            row["deck"] = label
            results.append(row)
            if os.path.exists(out):
                os.remove(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {"results": results, "compared": len(results),
            "exact": sum(1 for r in results if r.get("exact")),
            "selection_ok": sum(1 for r in results if r.get("selection_ok")),
            "unpaired_decks": unpaired}
