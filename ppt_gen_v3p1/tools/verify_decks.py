#!/usr/bin/env python3
"""Build a deck for every answer set and compare it with the deck Templafy made.

    python tools/verify_decks.py --library templates/firm \
                                 --decks ../data/decks --payloads ../data/payloads

    python tools/verify_decks.py --library templates/firm \
                                 --decks ../data/decks --payloads ../data/payloads --limit 10

Decks and answer sets pair up by name: `00_baseline.pptx` with
`00_baseline.json`. Each pair is built with the library's current rules and
compared slide by slide, the same comparison "Check against Templafy" does on
screen - so the two cannot disagree.

This is the regression net. Rules are authored by hand now, and the decks
Templafy really produced are the only objective answer to "is this right".
Run it after changing rules, before trusting them.

Reading the verdicts:
    MATCH                         right slides, right order, same text
    SELECTION OK, TEXT DIFFERS    right slides and order; some text differs -
                                  usually a binding or a date format, on the
                                  Values screen
    SELECTION OK - N data-driven  right slides; N slides Templafy regenerates
                                  per deck and could not be paired - not a
                                  selection mistake
    ORDER DIFFERS                 right slides, wrong order - drag on Rules
    SELECTION DIFFERS             a slide missing or extra - a condition to fix
    BUILD FAILED                  the rules could not build this deck at all

Exits 1 only if a build failed. A text difference is not always ours to fix:
Templafy's own decks carry an unfilled {{ Form.ShortClientName }} on one slide.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import Template                                       # noqa: E402
from engine.verify import verify                                  # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(__doc__.splitlines()[2:]))
    ap.add_argument("--library", required=True,
                    help="a template folder, e.g. templates/firm")
    ap.add_argument("--decks", required=True,
                    help="folder of the decks Templafy produced")
    ap.add_argument("--payloads", required=True,
                    help="folder of the answer sets, named to match the decks")
    ap.add_argument("--limit", type=int, default=None,
                    help="only the first N pairs - a quick look before a full run")
    args = ap.parse_args()

    tpl = Template(args.library)
    if not os.path.exists(tpl.rules_path):
        sys.exit("%s has no rules yet - author them first" % args.library)

    r = verify(tpl, args.decks, args.payloads, limit=args.limit)

    print("%-30s %s" % ("DECK", "VERDICT"))
    for row in r["results"]:
        line = "%-30s %s" % (row["deck"], row["verdict"])
        if row.get("error"):
            line += "  - %s" % row["error"]
        print(line)

    print()
    print("%d compared   %d exact   %d with the right slides"
          % (r["compared"], r["exact"], r["selection_ok"]))
    if r["unpaired_decks"]:
        print("no answer set for: %s" % ", ".join(r["unpaired_decks"][:10]))

    failed = [row for row in r["results"] if row["verdict"] == "BUILD FAILED"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
