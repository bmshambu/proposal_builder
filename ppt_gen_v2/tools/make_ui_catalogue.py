"""Build the UI mockup's data from the real payloads, without committing them.

The mockup ships with synthetic slide names and tidy field names, because the
firm's template and payloads are confidential and `.gitignore` keeps them out.
Tidy names flatter the design: the real fields are whole form questions, up to
88 characters, one of them with spaces in it. A screen that looks right on
`AuditType` and falls apart on
`Is_this_a_new_audit_client_or_new_entities_for_existing_client_i_e_Expansion_of_Services`
has not been tested.

So: run this, and the mockup loads *your* fields and *your* values. It writes
`ui/catalogue.js`, which is gitignored. The mockup falls back to the synthetic
data when the file is absent, so a fresh clone still opens.

    python tools/make_ui_catalogue.py --payloads ../data/payloads

Slide names stay synthetic unless you pass --template, since the library is
confidential too. What the catalogue proves is that the *controls* cope.
"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from engine.rules import flatten                              # noqa: E402

MAX_LISTED = 60          # a value list longer than this is a text box, not a menu


def read_payloads(folder):
    out = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            out.append((os.path.splitext(os.path.basename(path))[0], json.load(fh)))
    if not out:
        raise SystemExit("no .json payloads in %s" % folder)
    return out


def catalogue(payloads):
    """field -> {kind, values, varies}. The values are the ones actually seen.

    `varies` matters: a field with one value across every payload cannot explain
    anything, so offering it as a condition invites a rule that is always true.
    `propose.py` already refuses to infer from such a field; the UI should be
    just as unwilling to let someone pick one by hand.
    """
    seen = {}
    for _label, payload in payloads:
        for field, value in flatten(payload).items():
            seen.setdefault(field, []).append(value)

    fields = {}
    for field, values in seen.items():
        uniq, order = [], set()
        for v in values:
            key = json.dumps(v, sort_keys=True)
            if key not in order:
                order.add(key)
                uniq.append(v)
        kinds = {type(v).__name__ for v in uniq}
        if kinds <= {"bool"}:
            kind = "bool"
        elif all(isinstance(v, str) and v.isdigit() and len(v) == 8 for v in uniq):
            kind = "date"
        elif len(uniq) <= MAX_LISTED:
            kind = "choice"
        else:
            kind = "text"
        fields[field] = {
            "kind": kind,
            "values": sorted(uniq, key=lambda v: str(v)) if kind in ("bool", "choice") else [],
            "eg": uniq[0],
            "varies": len(uniq) > 1,
            "seen": len(values),
        }
    return fields


def demo_blocks(fields):
    """A block list shaped like the real template, named from the real fields.

    Each yes/no question in this template turns a section on, so one block per
    boolean field is a faithful demo of the shape - and it is the case that
    stresses the UI, because the condition carries the whole 88-character
    question.
    """
    spine = [("cover", "Cover", ["ClientName", "DueDate"]),
             ("agenda", "Agenda", []),
             ("about_us", "About our firm", ["FirmName"]),
             ("our_team", "Your engagement team", ["LeadPartner"]),
             ("approach", "Our audit approach", []),
             ("scope", "Scope of services", ["ClientName"]),
             ("fees", "Fees", ["FeePlanning", "FeeInterim", "FeeYearEnd", "FeeTotal"]),
             ("contacts", "Contacts", ["LeadPartner", "City"])]

    blocks = [{"id": bid, "t": title, "ph": ph, "src": "marker"}
              for bid, title, ph in spine]
    deck = [{"id": bid} for bid, _t, _p in spine[:6]]     # fees/contacts go last

    for field, spec in sorted(fields.items()):
        if not spec["varies"]:
            continue
        # Client names are text that gets substituted into slides, not answers
        # that pick them. A demo block conditioned on
        # `FullClientName is "Umbrella Corporation"` would be nonsense dressed
        # up as an example. They stay in the catalogue for the Mapping screen.
        if "clientname" in field.lower().replace("_", ""):
            continue
        bid = _slug(field)
        title = field.replace("_", " ").strip()
        if spec["kind"] == "bool":
            blocks.append({"id": bid, "t": title, "ph": [], "src": "marker"})
            deck.append({"id": bid, "when": {"field": field, "eq": True}})
        elif spec["kind"] == "choice" and len(spec["values"]) <= 4:
            blocks.append({"id": bid, "t": title, "ph": [], "src": "marker"})
            deck.append({"id": bid,
                         "when": {"field": field, "eq": spec["values"][-1]}})
        else:                                   # city / sector / sub-sector
            blocks.append({"id": bid, "t": title, "ph": [], "src": "marker"})
            deck.append({"id": bid, "when": {"field": field, "exists": True}})

    deck += [{"id": "fees"}, {"id": "contacts"}]

    # A few slides deliberately left out of the deck, so the pool is not empty:
    # an author needs to see what is available and unused, which is half the
    # point of the screen.
    blocks += [{"id": "team_bios", "t": "Team biographies", "ph": ["LeadPartner"],
                "src": "title"},
               {"id": "scope_expansion", "t": "Scope - expansion of services",
                "ph": ["ClientName"], "src": "marker"},
               {"id": "appendix_method", "t": "Appendix A - methodology",
                "ph": [], "src": "marker"},
               {"id": "appendix_refs", "t": "Appendix B - references",
                "ph": [], "src": "marker"}]
    return blocks, deck


def _slug(field):
    out = "".join(c if c.isalnum() else "_" for c in field.lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:44] or "block"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--payloads", default=os.path.join(HERE, "..", "..", "data",
                                                       "payloads"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "ui", "catalogue.js"))
    ap.add_argument("--brand", default="",
                    help="firm name to show in the header. Deliberately not a "
                         "default: the palette is public, the client of this "
                         "work is not, and this file is the one that gets "
                         "committed")
    ap.add_argument("--presets", type=int, default=8,
                    help="how many whole payloads to include for one-click "
                         "loading in the Inputs panel (default 8)")
    args = ap.parse_args(argv)

    payloads = read_payloads(args.payloads)
    fields = catalogue(payloads)
    blocks, deck = demo_blocks(fields)

    # A handful of whole payloads, so the Inputs panel can load a real answer
    # set in one click instead of making someone set 19 controls by hand. Take
    # them spread across the list rather than the first few, which are all
    # near-identical baselines.
    step = max(1, len(payloads) // args.presets)
    presets = [{"label": label, "answers": flatten(payload)}
               for label, payload in payloads[::step][:args.presets]]

    # The backend already models many libraries - a template is a folder under
    # templates/, and every command takes its name. Only the UI was
    # single-library. Real templates are listed here; the illustrative ones are
    # flagged so the selector can show the model without claiming they exist.
    libraries = [{"id": "public_audit", "name": "Public audit proposal",
                  "description": "The live template, harvested from 70 decks",
                  "slides": len(blocks), "fields": len(fields), "real": True}]
    libraries += [
        {"id": "private_audit", "name": "Private company audit",
         "description": "not imported yet", "slides": 0, "fields": 0,
         "real": False},
        {"id": "advisory", "name": "Advisory proposal",
         "description": "not imported yet", "slides": 0, "fields": 0,
         "real": False}]

    data = {"fields": fields, "blocks": blocks, "deck": deck,
            "presets": presets, "libraries": libraries,
            "brand": {"name": args.brand} if args.brand else None,
            "payloads": len(payloads),
            "source": os.path.abspath(args.payloads)}
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("/* Generated by tools/make_ui_catalogue.py - DO NOT COMMIT.\n"
                 "   Built from real payloads, which are confidential. */\n")
        fh.write("window.CATALOGUE = ")
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write(";\n")

    longest = max(fields, key=len)
    fixed = [f for f, s in fields.items() if not s["varies"]]
    print("Wrote %s" % out)
    print("  %d field(s) from %d payload(s)" % (len(fields), len(payloads)))
    print("  longest field name: %d chars - %s" % (len(longest), longest))
    print("  %d block(s), %d in the demo deck" % (len(blocks), len(deck)))
    if fixed:
        print("  %d field(s) never vary and cannot be a condition: %s"
              % (len(fixed), ", ".join(fixed[:3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
