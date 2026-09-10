#!/usr/bin/env python3
"""Read a master library and the decks it should produce, and suggest the rules.

    python tools/suggest_rules.py --library templates/firm \
                                  --decks ../data/decks/0*.pptx \
                                  --payloads ../data/payloads

Prints markdown: the deck order, which slides stay on `always`, which need a
condition and what it is, what to bind, what to check. `--json out.json` also
writes a rules.json to read.

If the library already has rules, it reports **what changes** rather than
everything - a second batch of decks should surface the two new things, not
restate the forty already confirmed. `--all` forces the full walkthrough.

The thinking lives in `engine/suggest.py`, which the UI calls too, so the
report on screen and the report here cannot drift apart.
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import Library, Template                              # noqa: E402
from engine import suggest, suggest_md                            # noqa: E402
from engine.payloads import catalogue, load_payloads              # noqa: E402


def resolve_library(path):
    """-> (library .pptx, block map, rules deck or None)

    A template folder keeps its ids in `blocks.json` and its rules beside the
    deck. Opening the bare .pptx re-derives ids from slide titles, so the names
    printed would be *almost* the names on screen - every instruction plausible,
    none of them followable.
    """
    if os.path.isdir(path):
        tpl = Template(path)
        rules = None
        if os.path.exists(tpl.rules_path):
            with open(tpl.rules_path, encoding="utf-8") as fh:
                rules = json.load(fh)
        return tpl.library_path, tpl.raw_block_map, rules
    sidecar = os.path.join(os.path.dirname(os.path.abspath(path)), "blocks.json")
    block_map = None
    if os.path.exists(sidecar):
        with open(sidecar, encoding="utf-8") as fh:
            block_map = json.load(fh)
    return path, block_map, None


def rules_deck(spec):
    """rules.json -> [{id, when}] in order, the shape compare() expects."""
    blocks = spec.get("blocks") or {}
    order = list(spec.get("baseline") or [])
    for bid in blocks:
        if bid not in order:
            order.append(bid)
    out = []
    for bid in order:
        when = (blocks.get(bid) or {}).get("when", "always")
        out.append({"id": bid,
                    "when": None if when in (None, "always", {}) else when})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", required=True,
                    help="a template folder (preferred - its blocks.json makes "
                         "the ids match the UI) or a bare .pptx")
    ap.add_argument("--decks", required=True, nargs="+",
                    help="files, folders or globs. Trying a subset is normal")
    ap.add_argument("--payloads", help="folder of answer sets, named to match "
                                       "the decks")
    ap.add_argument("--all", action="store_true",
                    help="the full walkthrough even when rules already exist")
    ap.add_argument("--json", help="also write the suggested rules.json here")
    args = ap.parse_args(argv)

    lib_path, block_map, existing = resolve_library(args.library)

    targets = []
    for spec in args.decks:
        if os.path.isdir(spec):
            targets += sorted(glob.glob(os.path.join(spec, "*.pptx")))
        elif any(c in spec for c in "*?["):
            targets += sorted(glob.glob(spec))
        else:
            targets.append(spec)
    # `~$name.pptx` is PowerPoint's lock file, present whenever a deck is open.
    targets = [t for t in dict.fromkeys(targets)
               if t.lower().endswith(".pptx")
               and not os.path.basename(t).startswith("~$")]
    if not targets:
        raise SystemExit("no target decks matched %s" % " ".join(args.decks))

    labels = [os.path.splitext(os.path.basename(t))[0] for t in targets]
    payloads = {}
    if args.payloads:
        payloads, _unpaired = load_payloads(args.payloads, labels)

    try:
        report = suggest.analyse(lib_path, targets, payloads, block_map=block_map)
    except ValueError as exc:
        raise SystemExit(str(exc))

    if existing and not args.all:
        print(suggest_md.changes(report, suggest.compare(report,
                                                         rules_deck(existing))))
    else:
        with Library(lib_path, block_map=block_map) as lib:
            phs = sorted(lib.all_placeholders())
        print(suggest_md.full(report, placeholders=phs))

    if args.json:
        blocks = {}
        for bid in report["consensus"]:
            if bid in report["never"]:
                continue
            entry = report["found"].get(bid)
            blocks[bid] = {"slides": [bid],
                           "when": suggest.as_condition(entry, report["fields"])
                           if entry else "always"}
        out = {"baseline": [b for b in report["consensus"]
                            if b not in report["never"]],
               "blocks": blocks,
               "_comment": "SUGGESTED from %d deck(s) by tools/suggest_rules.py "
                           "- confirm every condition before trusting it."
                           % len(targets),
               "placeholders": (existing or {}).get("placeholders", {})}
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("\n_wrote %s — read it, do not paste it in unread._" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
