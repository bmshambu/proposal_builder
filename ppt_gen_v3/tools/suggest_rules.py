#!/usr/bin/env python3
"""Read a master library and the decks it should produce, and suggest the rules.

    python tools/suggest_rules.py --library fixtures/master.pptx \
                                  --decks fixtures/targets \
                                  --payloads fixtures/payloads

It prints the steps to follow in the UI: which slides are always in, which need
a condition and what that condition looks like, and which need a human. Pass
`--json out.json` to also write a rules.json you can read.

**A suggestion, never a conclusion.** Everything here is inferred from what the
decks happen to contain, so it is only as good as the decks you give it:

  - one deck cannot tell a conditional slide from a permanent one. Everything
    looks "always". Give it several.
  - a field that never varies across your payloads explains nothing, however
    well its values line up.
  - two answers can explain a slide equally well, and no amount of evidence
    separates them. Those are printed as needing a decision rather than
    guessed at - this is exactly where the previous version of this project
    hit its ceiling.

Without `--payloads` it still works, and gives you the deck order and which
slides vary. It just cannot say *why* they vary.
"""
import argparse
import difflib
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import Library                                        # noqa: E402
from engine import forensics as pf                                # noqa: E402
from engine.payloads import catalogue, load_payloads              # noqa: E402
from engine.rules import flatten                                  # noqa: E402
from engine.verify import align                                   # noqa: E402


def norm(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return re.sub(r"\s+", " ", str(value)).strip()


# ------------------------------------------------------------ reading decks
def read_library(path):
    """-> ([block ids in order], {slide index: block id}, loaded deck)"""
    with Library(path) as lib:
        order = [b.id for b in lib.ordered()]
    deck = pf.load_deck(path)
    if len(order) != len(deck["slides"]):
        raise SystemExit("the library has %d blocks but %d slides - cannot line "
                         "them up" % (len(order), len(deck["slides"])))
    by_index = {s["index"]: order[i] for i, s in enumerate(deck["slides"])}
    return order, by_index, deck


def deck_order(target_path, lib_deck, by_index):
    """Which blocks this deck contains, in its own order. -> (blocks, unmatched)

    Uses the same alignment `verify` uses, so a slide is paired by shape
    creationId where the ids survived and by layout and geometry where they did
    not. A deck built by something that regenerates shapes will match less well,
    and the count of unmatched slides says so rather than hiding it.
    """
    deck = pf.load_deck(target_path)
    pairs, only_target, _only_lib = align(deck["slides"], lib_deck["slides"])
    ordered = [by_index[b["index"]]
               for _a, b, _m, _s in sorted(pairs, key=lambda p: p[0]["index"])]
    seen, out = set(), []
    for bid in ordered:
        if bid not in seen:
            seen.add(bid)
            out.append(bid)
    methods = {}
    for _a, _b, how, _s in pairs:
        methods[how] = methods.get(how, 0) + 1
    return out, len(only_target), deck["slide_count"], methods


# --------------------------------------------------------------- inference
def consensus_order(orders, fallback):
    """One order every deck agrees with; the library breaks ties.

    Each deck gives a partial order over the blocks it contains. Together they
    are a precedence graph, and a topological sort of it is an order no deck
    contradicts. Where two blocks never appear together there is no evidence,
    so the library decides and the result stays deterministic.
    """
    rank = {bid: i for i, bid in enumerate(fallback)}
    after = {bid: set() for bid in fallback}
    for order in orders:
        known = [b for b in order if b in rank]
        for i, earlier in enumerate(known):
            for later in known[i + 1:]:
                after[later].add(earlier)

    out, placed, remaining = [], set(), set(fallback)
    while remaining:
        ready = [b for b in remaining if not (after[b] - placed)] or list(remaining)
        pick = min(ready, key=lambda b: rank[b])
        out.append(pick)
        placed.add(pick)
        remaining.discard(pick)
    return out


def _words(text):
    return [w for w in re.split(r"[^a-z0-9]+", str(text).lower()) if w]


def _name_sim(a, b):
    """How much two names share actual words. 0..1.

    Whole words only, and only words long enough to mean something. Character
    similarity was tried first and was worse than nothing: it scored
    `continuity` against `City` and `scope_expansion` against `Transition_lab`
    highly enough to print "the field name matches the slide" about two names
    that share nothing. A wrong reason is more damaging than no reason - it
    invites agreement.
    """
    aw, bw = set(_words(a)), set(_words(b))
    if not aw or not bw:
        return 0.0
    hits = 0
    for x in aw:
        for y in bw:
            if x == y or (len(min(x, y, key=len)) >= 4
                          and (x in y or y in x)):
                hits += 1
                break
    return hits / min(len(aw), len(bw))


def rank_candidates(bid, matches, fields):
    """Order equally-fitting answers by which is most likely meant. -> [(f,v,why)]

    When several answers fit the decks identically, the deck data has nothing
    left to say and picking the first one alphabetically is a coin toss dressed
    as a conclusion. These tie-breaks are *about the names*, not the evidence,
    so each one is reported as the reason rather than folded in silently:

      - a field named like the slide (`Quality` -> `quality`) usually is the
        one; so is a value named like it (`Expansion of Services` ->
        `scope_expansion`).
      - a section that appears when a checkbox is **off** is rare. Real rules
        turn things on, so `Transition_lab is false` is ranked below a
        different field that fits just as well.

    Both are heuristics and both are wrong sometimes, which is why the
    alternatives stay on screen next to the suggestion.
    """
    scored = []
    for field, value in matches:
        why, score = [], 0.0
        fsim = _name_sim(bid, field)
        vsim = 0.0 if value is None else _name_sim(bid, value)
        if fsim >= 0.34:
            score += 2 * fsim
            why.append("field name matches the slide")
        if vsim >= 0.34:
            score += vsim
            why.append("value matches the slide name")
        if (fields.get(field, {}).get("kind") == "bool"
                and norm(value) == "false"):
            score -= 1.5
            why.append("but it fires when the box is OFF, which is unusual")
        scored.append((score, field, value, "; ".join(why)))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(f, v, w) for _s, f, v, w in scored]


def explain(block_decks, payloads, all_decks, fields):
    """block -> the answer that explains it. -> (found, ambiguous, unexplained)"""
    by_value = {}
    for deck in all_decks:
        for field, value in flatten(payloads[deck]).items():
            by_value.setdefault((field, norm(value)), set()).add(deck)

    varies = {f for f in {k[0] for k in by_value}
              if len({norm(flatten(p).get(f)) for p in payloads.values()}) > 1}

    found, ambiguous, unexplained = {}, {}, []
    for bid, decks in block_decks.items():
        present = set(decks) & all_decks
        if present == all_decks or not present:
            continue
        matches = sorted((f, v) for (f, v), d in by_value.items()
                         if d == present and f in varies)
        if not matches:
            # An `exists` rule looks different: the slide is in exactly the
            # decks where some field is answered at all. Worth checking before
            # giving up, because "answered or not" is a common real rule.
            #
            # Several fields are usually blank in the same decks, so these need
            # ranking as much as the value rules do - `industry_credentials`
            # and `local_office` are separated by their names, not by the data.
            answered_by = [f for f in sorted(varies)
                           if {d for d in all_decks
                               if norm(flatten(payloads[d]).get(f) or "") != ""}
                           == present]
            if answered_by:
                ranked = rank_candidates(bid, [(f, None) for f in answered_by],
                                         fields)
                field, _v, why = ranked[0]
                found[bid] = (field, None, "is answered", why)
                if len(ranked) > 1:
                    ambiguous[bid] = ranked
            else:
                unexplained.append((bid, sorted(present)))
        elif len(matches) == 1:
            found[bid] = (matches[0][0], matches[0][1], "is", "")
        else:
            ranked = rank_candidates(bid, matches, fields)
            field, value, why = ranked[0]
            found[bid] = (field, value, "is", why)
            ambiguous[bid] = ranked
    return found, ambiguous, unexplained


def guess_binding(placeholder, fields):
    """A binding worth checking, or None. Deliberately timid.

    `{{ClientName}}` almost certainly means the payload's `FullClientName`, but
    "almost certainly" is how a template ends up printing the wrong client. A
    guess is offered as something to confirm, never applied.
    """
    if placeholder in fields:
        return placeholder, "exact"
    low = placeholder.lower()
    contains = [f for f in fields if low in f.lower() or f.lower() in low]
    return (contains[0], "similar name") if len(contains) == 1 else None


# ------------------------------------------------------------------ report
def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True, help="the master .pptx")
    ap.add_argument("--decks", required=True,
                    help="a target .pptx, or a folder of them")
    ap.add_argument("--payloads", help="folder of answer sets, named to match "
                                       "the decks")
    ap.add_argument("--json", help="also write the suggested rules.json here")
    args = ap.parse_args(argv)

    order, by_index, lib_deck = read_library(args.library)

    targets = ([args.decks] if args.decks.lower().endswith(".pptx")
               else sorted(glob.glob(os.path.join(args.decks, "*.pptx"))))
    targets = [t for t in targets if not os.path.basename(t).startswith("~$")]
    if not targets:
        raise SystemExit("no target decks in %s" % args.decks)

    labels, orders, block_decks, weak = [], [], {}, []
    print("STEP 1 — Slides")
    print("  %s: %d slides" % (os.path.basename(args.library), len(order)))
    for path in targets:
        label = os.path.splitext(os.path.basename(path))[0]
        labels.append(label)
        blocks, unmatched, total, methods = deck_order(path, lib_deck, by_index)
        orders.append(blocks)
        for bid in blocks:
            block_decks.setdefault(bid, []).append(label)
        how = ", ".join("%s %d" % kv for kv in sorted(methods.items())) or "-"
        print("  %-28s %2d/%-2d slides matched (%s)"
              % (label, len(blocks), total, how))
        if unmatched:
            weak.append((label, unmatched))
    for label, n in weak:
        print("  ! %s: %d slide(s) matched nothing in the library. They may be "
              "regenerated per deck, or the library may be missing them."
              % (label, n))

    all_decks = set(labels)
    never = [b for b in order if b not in block_decks]
    always = [b for b in order if set(block_decks.get(b, [])) == all_decks]
    varies = [b for b in order if b in block_decks
              and set(block_decks[b]) != all_decks]

    # --------------------------------------------------------- step 2
    payloads, fields = {}, {}
    if args.payloads:
        payloads, unpaired = load_payloads(args.payloads, labels)
        fields = catalogue(list(payloads.items()))
        fixed = [f for f, s in fields.items() if not s["varies"]]
        print("\nSTEP 2 — Questions")
        print("  %d answer set(s), %d field(s)" % (len(payloads), len(fields)))
        if fixed:
            print("  ! %d never vary and cannot be a condition: %s"
                  % (len(fixed), ", ".join(fixed[:4])))
        for label in unpaired:
            print("  ! no answer set matched deck %s - it cannot explain "
                  "anything" % label)
    elif len(targets) > 1:
        print("\nSTEP 2 — Questions")
        print("  none given. Pass --payloads to turn 'this slide varies' into "
              "'this slide appears when X is Y'.")

    # --------------------------------------------------------- step 3
    consensus = consensus_order(orders, order)
    print("\nSTEP 3 — Rules")
    if len(targets) == 1:
        print("  ! Only one deck. Everything in it looks permanent, because "
              "nothing has been seen to vary.")
        print("    Give several decks that differ, or these rules will build "
              "the same deck for everyone.")

    print("\n  Deck order — drag the rows until they read:")
    for i, bid in enumerate(consensus, 1):
        mark = " " if bid in block_decks else "-"
        print("    %s %2d  %s" % (mark, i, bid))
    if never:
        print("\n  Drag OUT of the deck (in none of these decks): %s"
              % ", ".join(never))

    print("\n  Leave on \"always\" (%d, in every deck):" % len(always))
    print("    %s" % ", ".join(always) if always else "    (none)")

    found, ambiguous, unexplained = ({}, {}, [])
    if payloads:
        found, ambiguous, unexplained = explain(
            {b: block_decks[b] for b in varies}, payloads, all_decks, fields)

    print("\n  Give these a condition (%d):" % len(varies))
    if not varies:
        print("    (none — every slide is in every deck)")
    for bid in varies:
        n = len(block_decks[bid])
        if bid not in found:
            print("    %-24s in %d of %d decks — needs a human"
                  % (bid, n, len(all_decks)))
            continue
        field, value, op, why = found[bid]
        cond = ("%s %s" % (op, value)) if value is not None else op
        print("    %-24s %-38s %-28s (%d of %d decks)"
              % (bid, field, cond, n, len(all_decks)))
        if why:
            print("      chosen because: %s" % why)
        # The alternatives belong here, next to the suggestion, not in a
        # footnote underneath. A choice you have to scroll to find is a choice
        # most people will not make.
        for f, v, _w in (ambiguous.get(bid) or [])[1:4]:
            print("      ? or %-36s %s"
                  % (f, "is answered" if v is None else "is %s" % v))
        if len(ambiguous.get(bid) or []) > 4:
            print("      ? …and %d more that fit these decks equally"
                  % (len(ambiguous[bid]) - 4))
    for bid, decks in unexplained:
        print("  ! %s: appears in %s and no single answer explains it. Left on "
              "\"always\"." % (bid, ", ".join(decks)))

    # --------------------------------------------------------- step 4
    with Library(args.library) as lib:
        placeholders = sorted(lib.all_placeholders())
    if placeholders:
        print("\nSTEP 4 — Values (%d placeholder(s))" % len(placeholders))
        for p in placeholders:
            guess = guess_binding(p, fields) if fields else None
            if guess:
                print("    %-22s field  %-30s (%s — confirm it)"
                      % ("{{%s}}" % p, guess[0], guess[1]))
            else:
                print("    %-22s no matching answer field — a literal, or an "
                      "external source" % ("{{%s}}" % p))
        print("    Anything left unbound prints as {{...}} in the deck.")

    # --------------------------------------------------------- step 5
    print("\nSTEP 5 — Check")
    print("    Build with each answer set and compare against its deck. The")
    print("    goal is MATCH; anything else names the slide to fix.")

    if args.json:
        blocks = {}
        for bid in consensus:
            if bid in never:
                continue
            if bid in found:
                field, value, op, _why = found[bid]
                # Values are compared as text everywhere, so "true" would work -
                # but a boolean field should read as a boolean in rules.json, or
                # a file written here and a file written by the UI describe the
                # same rule two different ways.
                if (value is not None
                        and fields.get(field, {}).get("kind") == "bool"):
                    value = (norm(value) == "true")
                when = ({"field": field, "exists": True} if value is None
                        else {"field": field, "eq": value})
            else:
                when = "always"
            blocks[bid] = {"slides": [bid], "when": when}
        rules = {"baseline": [b for b in consensus if b not in never],
                 "blocks": blocks,
                 "_comment": "SUGGESTED from %d deck(s) by tools/suggest_rules.py"
                             " - confirm every condition before trusting it."
                             % len(targets),
                 "placeholders": {}}
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rules, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("\n  wrote %s — read it, do not paste it in unread" % args.json)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
