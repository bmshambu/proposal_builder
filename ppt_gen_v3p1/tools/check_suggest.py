#!/usr/bin/env python3
"""Score the rule suggester against the fixture's known answer.

    python tools/check_suggest.py

The fixture is the only case where the right answer is written down, so it is
the only case where "the suggester is helpful" can mean something measurable
rather than something felt.

It asserts a floor, not perfection. Some slides genuinely cannot be separated
by three answer sets, and a suggester that scored 15/15 by guessing harder
would be worse than one that says "these two fit equally". What must not
happen is a regression: a change that makes the hints quietly worse while the
output still looks confident.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FX = os.path.join(ROOT, "fixtures")

# Every condition is separable given these three answer sets, so anything less
# than all of them is a real loss of quality. Ordering is checked too: the
# suggested deck order has to be the library's, which is what the fixture uses.
FLOOR = 15


def main():
    out = os.path.join(tempfile.mkdtemp(), "suggested.json")
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "suggest_rules.py"),
         "--library", os.path.join(FX, "master.pptx"),
         "--decks", os.path.join(FX, "targets"),
         "--payloads", os.path.join(FX, "payloads"),
         "--json", out],
        capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        print(proc.stdout[-2000:])
        print(proc.stderr[-2000:])
        return 1

    sug = json.load(open(out, encoding="utf-8"))
    ref = json.load(open(os.path.join(FX, "reference_rules.json"),
                         encoding="utf-8"))

    same = lambda a, b: json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    right = []
    for bid in ref["baseline"]:
        want = ref["blocks"][bid]["when"]
        got = (sug["blocks"].get(bid) or {}).get("when", "<missing>")
        ok = same(want, got)
        right.append(ok)
        if not ok:
            print("  MISS %-22s got %-46s want %s"
                  % (bid, json.dumps(got), json.dumps(want)))

    n = sum(right)
    print("  ok   conditions      %d of %d match the answer key" % (n, len(right)))

    order_ok = sug["baseline"] == ref["baseline"]
    print("  %-4s deck order      %s"
          % ("ok" if order_ok else "MISS",
             "as the library has it" if order_ok else sug["baseline"][:6]))

    # A single deck must NOT produce confident conditions: with nothing seen to
    # vary, everything is "always", and claiming otherwise would be invention.
    one = os.path.join(tempfile.mkdtemp(), "one.json")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "suggest_rules.py"),
         "--library", os.path.join(FX, "master.pptx"),
         "--decks", os.path.join(FX, "targets", "p1_new_client.pptx"),
         "--payloads", os.path.join(FX, "payloads"), "--json", one],
        capture_output=True, text=True, cwd=ROOT)
    single = json.load(open(one, encoding="utf-8"))
    invented = [b for b, s in single["blocks"].items() if s["when"] != "always"]
    solo_ok = not invented
    print("  %-4s one deck only   %s"
          % ("ok" if solo_ok else "MISS",
             "every slide left on always — nothing was seen to vary"
             if solo_ok else "invented conditions: %s" % invented))

    failed = (n < FLOOR) or not order_ok or not solo_ok
    print("\n%s (floor is %d of %d)"
          % ("FAILED" if failed else "passed", FLOOR, len(right)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
