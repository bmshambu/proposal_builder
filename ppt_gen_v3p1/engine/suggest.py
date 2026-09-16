"""Work out, from a library and the decks it should produce, what the rules are.

Two jobs, and the second matters more once a template is in use:

  `analyse()`  - read the decks, match their slides to library blocks, and work
                 out which answer explains each slide that varies.
  `compare()`  - set that against the rules already saved, so a second batch of
                 decks reports *what changed* rather than restating everything
                 an author has already confirmed. Re-reading forty settled rules
                 to find the two new ones is how a helper stops being used.

Everything here is a suggestion. The decks are evidence, not instruction, and
the honest failure modes are named rather than smoothed over:

  - one deck cannot tell a conditional slide from a permanent one,
  - a field that never varies across the payloads explains nothing,
  - two answers can fit identically, and no amount of deck-reading separates
    them. Those are reported as a choice, with the alternatives, because a
    confident wrong rule costs more than an obvious gap.
"""
import difflib
import os
import re

from . import forensics as pf
from .library import Library
from .rules import flatten
from .verify import align


def norm(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return re.sub(r"\s+", " ", str(value)).strip()


# ------------------------------------------------------------------ reading
def read_library(path, block_map=None):
    """-> ([block ids in order], {slide index: block id}, loaded deck)"""
    with Library(path, block_map=block_map) as lib:
        order = [b.id for b in lib.ordered()]
    deck = pf.load_deck(path)
    if len(order) != len(deck["slides"]):
        raise ValueError("the library has %d blocks but %d slides"
                         % (len(order), len(deck["slides"])))
    return order, {s["index"]: order[i] for i, s in enumerate(deck["slides"])}, deck


def deck_blocks(target_path, lib_deck, by_index):
    """Which library blocks a deck contains, in its own order.

    Uses the same alignment `verify` does, so slides pair by shape creationId
    where those survived and by layout and geometry where they did not. The
    count that matched nothing is returned rather than hidden: a deck built by
    something that regenerates shapes will match poorly, and every conclusion
    below rests on this step.
    """
    deck = pf.load_deck(target_path)
    pairs, only_target, _only_lib = align(deck["slides"], lib_deck["slides"])
    seen, out = set(), []
    for _a, b, _m, _s in sorted(pairs, key=lambda p: p[0]["index"]):
        bid = by_index[b["index"]]
        if bid not in seen:
            seen.add(bid)
            out.append(bid)
    methods = {}
    for _a, _b, how, _s in pairs:
        methods[how] = methods.get(how, 0) + 1
    return {"blocks": out, "unmatched": len(only_target),
            "slides": deck["slide_count"], "methods": methods}


# ---------------------------------------------------------------- inference
def consensus_order(orders, fallback):
    """One order every deck agrees with; the library breaks ties.

    Each deck gives a partial order over the blocks it holds. Together they are
    a precedence graph, and a topological sort of it contradicts no deck. Where
    two blocks never appear together there is no evidence either way, so the
    library decides and the result stays deterministic.
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


def name_sim(a, b):
    """How much two names share actual words. 0..1.

    Whole words only, and only words long enough to mean something. Character
    similarity was tried first and was worse than nothing: it scored
    `continuity` against `City` and `scope_expansion` against `Transition_lab`
    highly enough to claim the names matched. A wrong reason is more damaging
    than no reason - it invites agreement.
    """
    aw, bw = set(_words(a)), set(_words(b))
    if not aw or not bw:
        return 0.0
    hits = 0
    for x in aw:
        for y in bw:
            if x == y or (len(min(x, y, key=len)) >= 4 and (x in y or y in x)):
                hits += 1
                break
    return hits / min(len(aw), len(bw))


def rank_candidates(bid, matches, fields):
    """Order equally-fitting answers by which is most likely meant.

    When several fit the decks identically the data has nothing left to say, so
    these tie-breaks are about *names*, not evidence - and each reports itself
    rather than being folded in silently:

      - a field or value named like the slide usually is the one,
      - a section that appears when a checkbox is OFF is rare; real rules turn
        things on.

    Both are wrong sometimes, which is why the alternatives stay attached.
    """
    scored = []
    for field, value in matches:
        why, score = [], 0.0
        fsim = name_sim(bid, field)
        vsim = 0.0 if value is None else name_sim(bid, value)
        if fsim >= 0.34:
            score += 2 * fsim
            why.append("field name matches the slide")
        if vsim >= 0.34:
            score += vsim
            why.append("value matches the slide name")
        if fields.get(field, {}).get("kind") == "bool" and norm(value) == "false":
            score -= 1.5
            why.append("but it fires when the box is off, which is unusual")
        scored.append((score, field, value, "; ".join(why)))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"field": f, "value": v, "why": w} for _s, f, v, w in scored]


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
        if matches:
            ranked = rank_candidates(bid, matches, fields)
            found[bid] = dict(ranked[0], op="eq")
            if len(ranked) > 1:
                ambiguous[bid] = ranked
            continue
        # An `exists` rule looks different: present in exactly the decks where
        # some field is answered at all. Common enough to check before giving up.
        answered_by = [f for f in sorted(varies)
                       if {d for d in all_decks
                           if norm(flatten(payloads[d]).get(f) or "") != ""} == present]
        if answered_by:
            ranked = rank_candidates(bid, [(f, None) for f in answered_by], fields)
            found[bid] = dict(ranked[0], op="exists")
            if len(ranked) > 1:
                ambiguous[bid] = ranked
        else:
            unexplained.append({"block": bid, "decks": sorted(present)})
    return found, ambiguous, unexplained


def as_condition(entry, fields):
    """A `found` entry -> the `when` a rules.json would hold."""
    field, value = entry["field"], entry["value"]
    if entry["op"] == "exists" or value is None:
        return {"field": field, "exists": True}
    if fields.get(field, {}).get("kind") == "bool":
        # Values compare as text everywhere, so "true" would work - but a
        # boolean field should read as a boolean, or a rule written here and
        # one written in the UI describe the same thing two different ways.
        return {"field": field, "eq": norm(value) == "true"}
    return {"field": field, "eq": value}


# ------------------------------------------------------------------- report
def analyse(library_path, deck_paths, payloads, block_map=None, fields=None):
    """-> a structured account of what these decks say the rules are."""
    from .payloads import catalogue

    order, by_index, lib_deck = read_library(library_path, block_map)
    if fields is None:
        fields = catalogue(list(payloads.items()))

    decks, orders, block_decks = [], [], {}
    for path in deck_paths:
        label = os.path.splitext(os.path.basename(path))[0]
        info = deck_blocks(path, lib_deck, by_index)
        info["label"] = label
        decks.append(info)
        orders.append(info["blocks"])
        for bid in info["blocks"]:
            block_decks.setdefault(bid, []).append(label)

    labels = [d["label"] for d in decks]
    all_decks = set(labels) & set(payloads) if payloads else set()
    never = [b for b in order if b not in block_decks]
    always = [b for b in order if set(block_decks.get(b, [])) == set(labels)]
    varies = [b for b in order if b in block_decks
              and set(block_decks[b]) != set(labels)]

    found, ambiguous, unexplained = ({}, {}, [])
    if all_decks:
        found, ambiguous, unexplained = explain(
            {b: block_decks[b] for b in varies}, payloads, all_decks, fields)

    return {
        "library_blocks": order,
        "decks": decks,
        "labels": labels,
        "paired": sorted(all_decks),
        "unpaired": [l for l in labels if l not in all_decks],
        "block_decks": block_decks,
        "consensus": consensus_order(orders, order) if orders else list(order),
        "never": never, "always": always, "varies": varies,
        "found": found, "ambiguous": ambiguous, "unexplained": unexplained,
        "fields": fields,
        "fixed_fields": sorted(f for f, s in (fields or {}).items()
                               if not s.get("varies")),
    }


def compare(report, current_deck):
    """Set the findings against the rules already saved. -> what changed.

    `current_deck` is the UI's shape: [{id, when}] in order. The point is that
    a second batch of decks should report the two new things, not restate the
    forty an author already confirmed - so `settled` is counted and not listed.
    """
    have = {r["id"]: r.get("when") for r in (current_deck or [])}
    in_deck = set(have)
    fields = report["fields"]
    same = lambda a, b: _canon(a) == _canon(b)

    settled, new, changed, resolved = [], [], [], []
    for bid in report["varies"]:
        suggested = as_condition(report["found"][bid], fields) \
            if bid in report["found"] else None
        mine = have.get(bid)
        if bid not in in_deck:
            new.append({"block": bid, "suggested": suggested,
                        "reason": "not in the deck at all"})
        elif suggested is None:
            changed.append({"block": bid, "mine": mine, "suggested": None,
                            "reason": "varies across these decks, but no single "
                                      "answer explains it"})
        elif mine is None:
            new.append({"block": bid, "suggested": suggested,
                        "reason": "set to always, but it varies across these decks"})
        elif same(mine, suggested):
            settled.append(bid)
        else:
            changed.append({"block": bid, "mine": mine, "suggested": suggested,
                            "reason": "the decks point somewhere else"})

    # A rule that these decks no longer justify. Not necessarily wrong - the
    # batch may simply not exercise it - so it is raised as a question.
    for bid in report["always"]:
        if have.get(bid) is not None:
            resolved.append({"block": bid, "mine": have[bid],
                             "reason": "in every one of these decks, so this "
                                       "batch does not exercise the rule"})

    missing = [b for b in report["library_blocks"]
               if b not in in_deck and b in report["block_decks"]]
    extra = [b for b in in_deck if b in report["never"]]
    return {"settled": settled, "new": new, "changed": changed,
            "questions": resolved, "not_in_deck": missing,
            "in_deck_but_unseen": extra,
            "nothing_to_do": not (new or changed or missing)}


def _canon(when):
    import json
    if when in (None, "always", {}):
        return "always"
    # `true` and "true" are the same rule to rules.py, and reporting them as a
    # difference would send an author to change something that is already right.
    return json.dumps({k: norm(v) if not isinstance(v, list) else [norm(x) for x in v]
                       for k, v in sorted(when.items())}, sort_keys=True)
