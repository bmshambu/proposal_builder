"""Does our deck match the one Templafy produced from the same answers?

This is the acceptance test for the whole exercise. Everything else checks that
a deck is *valid*; this checks that it is *right* — same slides, same order, for
the same payload — against the only authority there is: the decks Templafy
actually generated.

It is the question worth asking before anyone retires Templafy for a template,
and the only one whose answer is not a matter of opinion.

Slides are compared by identity, not by text: our deck says "Acme Holdings" and
the reference says "Example Corporation", and neither is wrong. What must match
is *which slides* and *in what order*.

--------------------------------------------------------------------------
NOT YET TRUSTWORTHY
--------------------------------------------------------------------------
On the synthetic fixtures this reported a five-slide build twice for a deck
that is provably seven slides — the same build, run directly and repeated six
times, is deterministic and correct. The discrepancy has not been explained, so
a mismatch reported here may be this module's fault rather than the template's.

Treat a "does not match" as a prompt to look, never as a verdict. It is not
wired into `check`, and no decision should rest on it until the inconsistency
is understood.
"""
import os
import tempfile

from .identity import geometry_key, slide_identity
from .library import Library


def _index_library(template):
    """identity -> block id, for every slide in the library.

    Geometry is a fallback for decks without creationIds, kept only where it
    names one block — slides built from one layout share it, and a wrong match
    would report a difference that is not there.
    """
    with template.open_library() as lib:
        by_identity, geo_seen = {}, {}
        for block in lib.ordered():
            raw = lib.read_bytes(block.part)
            key, _how = slide_identity(raw)
            by_identity.setdefault(key, block.id)
            geo = geometry_key(raw)
            if geo:
                geo_seen.setdefault(geo, []).append(block.id)
    by_geometry = {g: ids[0] for g, ids in geo_seen.items() if len(ids) == 1}
    return by_identity, by_geometry


def _block_sequence(path, by_identity, by_geometry):
    """The blocks a deck contains, in order. Unrecognised slides become None."""
    sequence = []
    with Library(path) as deck:
        for part in deck.slide_parts:
            raw = deck.read_bytes(part)
            key, _how = slide_identity(raw)
            bid = by_identity.get(key)
            if bid is None:
                geo = geometry_key(raw)
                bid = by_geometry.get(geo) if geo else None
            sequence.append(bid)
    return sequence


def compare_sequences(expected, actual):
    """-> dict describing how our deck differs from the reference.

    Order is reported separately from content: a deck with the right slides in
    the wrong order is a different problem from one missing a section, and
    conflating them hides which.
    """
    exp_set, act_set = set(expected) - {None}, set(actual) - {None}
    missing = [b for b in expected if b is not None and b not in act_set]
    extra = [b for b in actual if b is not None and b not in exp_set]
    common_expected = [b for b in expected if b in act_set]
    common_actual = [b for b in actual if b in exp_set]
    return {
        "expected": len(expected), "actual": len(actual),
        "missing": missing, "extra": extra,
        "unrecognised": sum(1 for b in expected if b is None),
        "reordered": common_expected != common_actual,
        "exact": (not missing and not extra
                  and common_expected == common_actual
                  and len(expected) == len(actual)),
    }


def verify(template, decks_dir, payloads_dir, build_fn=None, limit=None):
    """Build a deck per payload and compare it with Templafy's. -> report dict."""
    import glob
    import json

    from .assemble import build_template
    from .propose import load_payloads

    build_fn = build_fn or build_template
    decks = {os.path.splitext(os.path.basename(p))[0]: p
             for p in sorted(glob.glob(os.path.join(str(decks_dir), "*.pptx")))
             if not os.path.basename(p).startswith("~$")}
    payloads, unpaired = load_payloads(payloads_dir, sorted(decks))
    by_identity, by_geometry = _index_library(template)

    results, tmp = [], tempfile.mkdtemp(prefix="pptgen2_verify_")
    try:
        for label in sorted(payloads)[:limit]:
            reference = decks[label]
            out = os.path.join(tmp, label + ".pptx")
            try:
                build_fn(template, payloads[label], out)
            except Exception as exc:
                results.append({"deck": label, "error": "%s: %s"
                                % (type(exc).__name__, exc), "exact": False})
                continue
            expected = _block_sequence(reference, by_identity, by_geometry)
            actual = _block_sequence(out, by_identity, by_geometry)
            row = compare_sequences(expected, actual)
            row["deck"] = label
            results.append(row)
            os.remove(out)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    exact = [r for r in results if r.get("exact")]
    return {"results": results, "compared": len(results), "exact": len(exact),
            "unpaired_decks": unpaired}
