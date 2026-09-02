#!/usr/bin/env python3
"""
map_decks.py  -  learn the selection + token rules from (payload -> deck) pairs.

INPUT
    * The 265-slide master template (.pptx).
    * A set of generated decks (.pptx), one per payload.
    * The payloads (.json) that produced them.

WHAT IT DOES
    1. Matches every slide in each generated deck back to its origin slide in
       the master (by shape creationId GUIDs; text similarity as fallback).
    2. Records, per payload, WHICH master slides were kept (the selection) and
       in what order.
    3. Correlates selection to payload fields: for each master slide, works out
       whether it is ALWAYS kept, or kept only when some field == some value.
    4. Detects injected TOKENS: text that appears in a generated slide but not
       its master origin, matched against payload values (e.g. client, date).
    5. Writes a machine-readable mapping_spec.json and a human mapping_report.md.

It is incremental by design: add more (payload, deck) pairs and re-run to
sharpen the rules - exactly the "keep running, logic updates" loop.

PRIVACY / SAFETY
    Read-only, standard-library only, no network calls. Run it locally on the
    confidential decks; nothing leaves the machine.

USAGE
    python map_decks.py --master master_265.pptx \
                        --decks ./decks --payloads ./payloads \
                        --out ./mapping

    Pairing of decks<->payloads:
      * By default, a deck and payload are paired when their filename stems
        match (e.g. 01_audit.pptx  <->  01_audit.json), else by sorted order
        when the counts are equal. The chosen pairing is printed - verify it.
      * Or pass --manifest pairs.csv with header:  pptx,payload
"""

import argparse
import csv
import glob
import json
import os
import sys

import pptx_forensics as pf

MATCH_THRESHOLD = 0.34   # min similarity to accept a slide<->master match


# ---------------------------------------------------------------- payloads
def flatten(obj, prefix=""):
    """Flatten nested JSON to {dotted.key: "string value"} for scalar leaves."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, prefix + str(k) + "." if not prefix else prefix + str(k) + "."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, prefix + str(i) + "."))
    else:
        if obj is not None and str(obj) != "":
            out[prefix.rstrip(".")] = str(obj)
    return out


def load_payload(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw, flatten(raw)


# ---------------------------------------------------------------- pairing
def pair_files(decks_dir, payloads_dir, manifest):
    decks = sorted(glob.glob(os.path.join(decks_dir, "*.pptx")), key=pf.natural_key)
    payloads = sorted(glob.glob(os.path.join(payloads_dir, "*.json")), key=pf.natural_key)
    if manifest:
        pairs = []
        with open(manifest, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pairs.append((os.path.join(decks_dir, row["pptx"]),
                              os.path.join(payloads_dir, row["payload"])))
        return pairs
    # match by stem
    pay_by_stem = {os.path.splitext(os.path.basename(p))[0]: p for p in payloads}
    pairs, used = [], set()
    for d in decks:
        stem = os.path.splitext(os.path.basename(d))[0]
        if stem in pay_by_stem:
            pairs.append((d, pay_by_stem[stem]))
            used.add(pay_by_stem[stem])
    if len(pairs) != len(decks):
        # fall back to sorted-order zip if counts line up
        if len(decks) == len(payloads):
            pairs = list(zip(decks, payloads))
        else:
            sys.exit("Could not pair decks with payloads by name, and counts "
                     "differ (%d decks, %d payloads). Use --manifest."
                     % (len(decks), len(payloads)))
    return pairs


# ---------------------------------------------------------------- matching
def match_deck_to_master(gen_deck, master):
    """For each generated slide, find best master slide. Returns list of matches
    and the ordered set of matched master indices (the 'selection')."""
    m_slides = master["slides"]
    matches = []
    for g in gen_deck["slides"]:
        best_idx, best_method, best_score = None, None, 0.0
        for m in m_slides:
            method, score = pf.slide_similarity(g, m)
            if score > best_score:
                best_idx, best_method, best_score = m["index"], method, score
        accepted = best_score >= MATCH_THRESHOLD
        matches.append({
            "gen_index": g["index"],
            "master_index": best_idx if accepted else None,
            "method": best_method,
            "score": round(best_score, 3),
            "accepted": accepted,
            "gen_text_preview": g["text"][:80],
        })
    selected = [m["master_index"] for m in matches if m["accepted"] and m["master_index"]]
    return matches, selected


# ---------------------------------------------------------------- token detection
def detect_tokens(gen_deck, master, matches, payload_flat):
    """Find words that a generated slide added vs its master origin, and label
    them if they equal a payload value."""
    m_by_idx = {m["index"]: m for m in master["slides"]}
    # value -> field name (reverse of payload) for labeling
    val_to_field = {}
    for k, v in payload_flat.items():
        for token in (v, v.strip()):
            val_to_field.setdefault(token.lower(), k)
    tokens = []
    for match, g in zip(matches, gen_deck["slides"]):
        if not match["accepted"] or not match["master_index"]:
            continue
        m = m_by_idx.get(match["master_index"])
        if not m:
            continue
        added = pf.word_set(g["text"]) - pf.word_set(m["text"])
        # try whole-value matches first (multi-word values)
        gtext = g["text"].lower()
        for val, field in val_to_field.items():
            if val and val in gtext and val not in (m["text"].lower()):
                tokens.append({"gen_index": g["index"],
                               "master_index": m["index"],
                               "field": field, "value": val,
                               "match": "value-in-text"})
        # single-word additions that map to a field value
        for w in added:
            if w in val_to_field:
                tokens.append({"gen_index": g["index"],
                               "master_index": m["index"],
                               "field": val_to_field[w], "value": w,
                               "match": "added-word"})
    return tokens


# ---------------------------------------------------------------- correlation
def correlate(per_deck, master_count):
    """For each master slide, decide: always kept / conditional on field=value /
    unclear. Uses the selection sets across all payloads."""
    # gather, per master slide, the set of payload-indices that kept it
    n = len(per_deck)
    kept_in = {i: set() for i in range(1, master_count + 1)}
    for d_i, d in enumerate(per_deck):
        for idx in set(d["selected"]):
            if idx in kept_in:
                kept_in[idx].add(d_i)

    # index field values per payload
    field_values = {}   # field -> list-of-value-per-payload
    all_fields = set()
    for d in per_deck:
        all_fields.update(d["payload_flat"].keys())
    for f in all_fields:
        field_values[f] = [d["payload_flat"].get(f) for d in per_deck]

    rules = {}
    for idx in range(1, master_count + 1):
        keepers = kept_in[idx]
        if not keepers:
            rules[idx] = {"rule": "never_used"}
            continue
        if len(keepers) == n:
            rules[idx] = {"rule": "always"}
            continue
        # look for a field whose value(s) appear ONLY in the keeper payloads.
        # Guard against high-cardinality fields (e.g. client name, unique per
        # payload) trivially "explaining" any subset: prefer fields whose
        # deciding value repeats across payloads and that have few distinct
        # values overall.
        best = None  # (score_tuple, field, sorted_values, support)
        for f, vals in field_values.items():
            keeper_vals = {vals[i] for i in keepers}
            other_vals = {vals[i] for i in range(n) if i not in keepers}
            if None in keeper_vals:
                continue                      # field missing in some keeper
            if keeper_vals & other_vals:
                continue                      # value not exclusive to keepers
            distinct = len({v for v in vals if v is not None})
            # support = fewest payloads any deciding value covers (>=2 == solid)
            support = min(sum(1 for i in range(n) if vals[i] == v)
                          for v in keeper_vals)
            score = (support, -distinct)      # repeated value first, then low card
            if best is None or score > best[0]:
                best = (score, f, sorted(keeper_vals), support)
        if best:
            rules[idx] = {"rule": "conditional", "field": best[1],
                          "values": best[2],
                          "confidence": "high" if best[3] >= 2 else "low",
                          "kept_in_payloads": sorted(keepers)}
        else:
            rules[idx] = {"rule": "unclear", "kept_in_payloads": sorted(keepers)}
    return rules


# ---------------------------------------------------------------- reporting
def write_report(out_dir, master, per_deck, rules, all_tokens):
    lines = ["# Deck mapping report", ""]
    lines.append("Master: `%s`  (%d slides)" % (master["name"], master["slide_count"]))
    lines.append("Payload/deck pairs analysed: **%d**" % len(per_deck))
    lines.append("")

    # match-quality summary (so you can trust the results)
    lines.append("## Match quality")
    for d in per_deck:
        acc = sum(1 for m in d["matches"] if m["accepted"])
        low = [m for m in d["matches"] if m["accepted"] and m["score"] < 0.6]
        cre = sum(1 for m in d["matches"] if m["method"] == "creationId" and m["accepted"])
        lines.append("- `%s`: %d/%d slides matched (%d via creationId), "
                     "%d unmatched, %d low-confidence(<0.6)"
                     % (d["deck"], acc, len(d["matches"]),
                        cre, len(d["matches"]) - acc, len(low)))
    lines.append("")
    lines.append("> creationId matches are reliable. Many text-only or "
                 "low-confidence matches mean the master shapes lack creationIds "
                 "- inspect those slides manually.")
    lines.append("")

    # the selection rule per master slide
    always = [i for i, r in rules.items() if r["rule"] == "always"]
    never = [i for i, r in rules.items() if r["rule"] == "never_used"]
    cond = {i: r for i, r in rules.items() if r["rule"] == "conditional"}
    unclear = {i: r for i, r in rules.items() if r["rule"] == "unclear"}

    lines.append("## Selection rules (master slide -> when it is kept)")
    lines.append("- **Always kept (static skeleton):** %d slides -> %s"
                 % (len(always), _rng(always)))
    lines.append("- **Never used in these payloads:** %d slides -> %s"
                 % (len(never), _rng(never)))
    lines.append("- **Conditional (driven by a field):** %d slides" % len(cond))
    lines.append("- **Unclear (needs more payloads):** %d slides -> %s"
                 % (len(unclear), _rng(sorted(unclear))))
    lines.append("")
    if cond:
        lines.append("### Conditional slides")
        lines.append("")
        lines.append("| Master slide | Kept when | Value(s) | Confidence |")
        lines.append("|---|---|---|---|")
        for i in sorted(cond):
            r = cond[i]
            lines.append("| %d | `%s` | %s | %s |"
                         % (i, r["field"], ", ".join(map(str, r["values"])),
                            r.get("confidence", "?")))
        lines.append("")

    # tokens
    lines.append("## Detected injected tokens (dynamic text)")
    if all_tokens:
        seen = set()
        lines.append("| Payload field | Example value | Seen on master slides |")
        lines.append("|---|---|---|")
        byfield = {}
        for t in all_tokens:
            byfield.setdefault(t["field"], {"vals": set(), "slides": set()})
            byfield[t["field"]]["vals"].add(t["value"])
            byfield[t["field"]]["slides"].add(t["master_index"])
        for f, info in sorted(byfield.items()):
            ex = sorted(info["vals"])[0]
            lines.append("| `%s` | %s | %s |"
                         % (f, ex, _rng(sorted(info["slides"]))))
    else:
        lines.append("_None detected. Client/date may be inside images, or "
                     "text is stored differently - inspect a known-variable "
                     "slide manually._")
    lines.append("")

    lines.append("## Next")
    lines.append("- Add more payloads that vary ONE field at a time to resolve "
                 "the 'unclear' slides.")
    lines.append("- Cross-check these inferred rules against the master's "
                 "customXml data sources (item118/124/126) - those hold the "
                 "authoritative binding tables.")

    report = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "mapping_report.md"), "w", encoding="utf-8") as fh:
        fh.write(report)
    return report


def _rng(nums):
    """Compress [1,2,3,7,8] -> '1-3, 7-8'."""
    if not nums:
        return "(none)"
    nums = sorted(nums)
    out, start, prev = [], nums[0], nums[0]
    for x in nums[1:]:
        if x == prev + 1:
            prev = x
            continue
        out.append("%d-%d" % (start, prev) if start != prev else str(start))
        start = prev = x
    out.append("%d-%d" % (start, prev) if start != prev else str(start))
    return ", ".join(out)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Learn selection + token rules from "
                                             "(payload -> generated deck) pairs.")
    ap.add_argument("--master", required=True, help="The 265-slide master .pptx")
    ap.add_argument("--decks", required=True, help="Folder of generated .pptx files")
    ap.add_argument("--payloads", required=True, help="Folder of .json payloads")
    ap.add_argument("--manifest", help="Optional CSV (pptx,payload) pairing")
    ap.add_argument("--out", default="mapping", help="Output folder")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print("Loading master: %s" % args.master)
    master = pf.load_deck(args.master)
    print("  master slides: %d" % master["slide_count"])

    pairs = pair_files(args.decks, args.payloads, args.manifest)
    print("\nPairing (verify this is correct):")
    for d, p in pairs:
        print("  %-40s <-  %s" % (os.path.basename(d), os.path.basename(p)))

    per_deck = []
    for deck_path, payload_path in pairs:
        gen = pf.load_deck(deck_path)
        raw, flat = load_payload(payload_path)
        matches, selected = match_deck_to_master(gen, master)
        tokens = detect_tokens(gen, master, matches, flat)
        per_deck.append({
            "deck": os.path.basename(deck_path),
            "payload": os.path.basename(payload_path),
            "payload_flat": flat,
            "matches": matches,
            "selected": selected,
            "tokens": tokens,
        })
        print("  analysed %s: %d/%d slides matched"
              % (os.path.basename(deck_path),
                 sum(1 for m in matches if m["accepted"]), len(matches)))

    rules = correlate(per_deck, master["slide_count"])
    all_tokens = [t for d in per_deck for t in d["tokens"]]

    spec = {
        "master": {"name": master["name"], "slides": master["slide_count"]},
        "payload_count": len(per_deck),
        "selection_rules": {str(k): v for k, v in rules.items()},
        "per_deck": [{"deck": d["deck"], "payload": d["payload"],
                      "selected_master_slides": sorted(set(d["selected"])),
                      "payload_flat": d["payload_flat"],
                      "tokens": d["tokens"]} for d in per_deck],
    }
    with open(os.path.join(args.out, "mapping_spec.json"), "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)

    report = write_report(args.out, master, per_deck, rules, all_tokens)
    print("\n" + report)
    print("[wrote %s/mapping_spec.json and %s/mapping_report.md]"
          % (args.out, args.out))


if __name__ == "__main__":
    main()
