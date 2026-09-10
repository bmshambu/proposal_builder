"""Pairing generated decks with the answers that produced them.

This lived in `propose.py` in v2, where the job was *inferring* rules by diffing
70 decks. That whole path stays behind: v3's rules are authored, not harvested.

But `verify` still needs it. It is the only objective check that hand-authored
rules produce the right deck, and it works by building from a payload and
comparing with the deck Templafy built from the same one - so it has to know
which payload goes with which deck. That is the entire dependency, and it is
small enough to own rather than to import a harvester for.

The field catalogue the UI's forms and condition editors are built from comes
from here too: `catalogue()` reads what the answers actually contain, so a form
and a rule can never disagree about a field's name or its values.
"""
import json
import os
import re

from .rules import flatten

MANY_VALUES = 60          # past this, a value list is a search box, not a menu


def load_payloads(folder, deck_labels):
    """Pair each deck with its payload by file name.

    `01_x.pptx` <- `01_x.json`, or a shared numeric prefix.
    -> ({label: answers}, [labels with no payload])
    """
    if not folder or not os.path.isdir(str(folder)):
        return {}, list(deck_labels)
    folder = str(folder)
    files = {os.path.splitext(f)[0]: os.path.join(folder, f)
             for f in os.listdir(folder) if f.endswith(".json")}
    by_prefix = {}
    for stem in files:
        m = re.match(r'(\d+)', stem)
        if m:
            by_prefix.setdefault(m.group(1), stem)

    paired, unpaired = {}, []
    for label in deck_labels:
        stem = label if label in files else None
        if stem is None:
            m = re.match(r'(\d+)', label)
            if m and m.group(1) in by_prefix:
                stem = by_prefix[m.group(1)]
        if stem is None:
            unpaired.append(label)
            continue
        with open(files[stem], encoding="utf-8") as fh:
            paired[label] = json.load(fh)
    return paired, unpaired


def read_all(folder):
    """[(label, answers)] for every payload in a folder, by name."""
    out = []
    if not folder or not os.path.isdir(str(folder)):
        return out
    for name in sorted(os.listdir(str(folder))):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(str(folder), name), encoding="utf-8") as fh:
            out.append((os.path.splitext(name)[0], json.load(fh)))
    return out


def catalogue(payloads):
    """field -> {kind, values, eg, varies, seen}, from the answers themselves.

    One list, feeding both the build form and the condition editor. Typing a
    field name by hand is how a rule silently stops matching, so the UI never
    offers the chance.

    `varies` earns its place: a field answered identically in every payload
    cannot explain anything, and a condition on it is always true or always
    false. The UI refuses to pretend otherwise.
    """
    seen = {}
    for _label, payload in payloads:
        for field, value in flatten(payload).items():
            seen.setdefault(field, []).append(value)

    fields = {}
    for field, values in seen.items():
        uniq, known = [], set()
        for v in values:
            key = json.dumps(v, sort_keys=True)
            if key not in known:
                known.add(key)
                uniq.append(v)
        kinds = {type(v).__name__ for v in uniq}
        if kinds <= {"bool"}:
            kind = "bool"
        elif all(isinstance(v, str) and v.isdigit() and len(v) == 8 for v in uniq):
            kind = "date"
        elif len(uniq) <= MANY_VALUES:
            kind = "choice"
        else:
            kind = "text"
        fields[field] = {
            "kind": kind,
            "values": sorted(uniq, key=lambda v: str(v))
                      if kind in ("bool", "choice") else [],
            "eg": uniq[0],
            "varies": len(uniq) > 1,
            "seen": len(values),
        }
    return fields
