#!/usr/bin/env python3
"""
make_payloads.py  -  (re)generate the OFAT training set + held-out test set.

Writes all payload JSONs into data/payloads/ using the exact Templafy schema in
app/schema.py. Safe to re-run — it overwrites the generated files.

  TRAINING (used to build the harvested library):
    00_baseline                     all toggles off, Audit only / New Audit Client
                                    / Technology-Software / New York
    01..11  <toggle>                one boolean toggle on (one per optional section)
    12_audit_and_tax                audit_or_tax = "Audit & Tax"
    13_expansion                    new_or_expansion = "Expansion of Services"
    20..29  sector_<name>           each non-Technology sector (+ its first sub-sector)
    40..42  sub_<name>              Technology sub-sector variants (isolates sub-sector)

  TEST (held out — names start with test_, excluded from harvesting):
    test_combo_*                    unseen COMBINATIONS of the above + new client/date,
                                    to validate that the additive logic generalizes.

Usage:  python make_payloads.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import schema, config

# Baseline = every OFAT payload's reference point. Change ONE thing per variant.
BASE = {
    "full_client_name": "Example Corporation", "short_client_name": "Example",
    "due_date": "2026-11-30", "audit_or_tax": "Audit only",
    "new_or_expansion": "New Audit Client", "sector": "Technology",
    "sub_sector": "Software", "city": "New York",
}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def write(name, overrides):
    form = dict(BASE)
    form["name"] = name
    form.update(overrides)
    payload = schema.build_payload(form)
    path = os.path.join(str(config.PAYLOADS_DIR), name + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return name


def main():
    written = []
    # 00 baseline
    written.append(write("00_baseline", {}))

    # 01..11 one boolean toggle each (order follows schema.BOOLEAN_FIELDS)
    for i, (fkey, (_tkey, _label)) in enumerate(schema.BOOLEAN_FIELDS.items(), start=1):
        written.append(write("%02d_%s" % (i, fkey), {fkey: "on"}))

    # 12,13 the two remaining single-value enums
    written.append(write("12_audit_and_tax", {"audit_or_tax": "Audit & Tax"}))
    written.append(write("13_expansion", {"new_or_expansion": "Expansion of Services"}))

    # 20.. sector sweep (sector + its first sub-sector co-vary)
    i = 20
    for sec in schema.SECTORS:
        if sec == "Technology":
            continue
        sub = schema.SUBSECTORS_BY_SECTOR[sec][0]
        written.append(write("%d_sector_%s" % (i, _slug(sec)),
                             {"sector": sec, "sub_sector": sub}))
        i += 1

    # 40.. Technology sub-sector variants (isolates the sub-sector effect)
    j = 40
    for sub in schema.SUBSECTORS_BY_SECTOR["Technology"]:
        if sub == "Software":
            continue
        written.append(write("%d_sub_%s" % (j, _slug(sub)), {"sub_sector": sub}))
        j += 1

    train_n = len(written)

    # ---- held-out TEST set --------------------------------------------------
    test = []

    # (a) PASSING cases: unseen COMBINATIONS but the STANDARD client/date, so the
    #     data-driven slides carry the same content as training -> expect MATCH.
    #     These demonstrate "payloads like these reproduce perfectly."
    test.append(write("test_pass_transitionlab_chicago_audittax", {
        "transition_lab": "on", "city": "Chicago", "audit_or_tax": "Audit & Tax"}))
    test.append(write("test_pass_peerreview_healthcare", {
        "peer_review": "on", "sector": "Healthcare", "sub_sector": "Healthcare"}))
    test.append(write("test_pass_aboutkpmg_quality_expansion", {
        "about_kpmg": "on", "quality": "on",
        "new_or_expansion": "Expansion of Services"}))

    # (b) NEW-CLIENT combinations: exercise selection composition + tokens, and
    #     expose the ~5 data-driven slides (fees/profile/RFP) that need the data
    #     source. These characterise "what still differs, and why."
    test.append(write("test_combo_transitionlab_peerreview_chicago", {
        "transition_lab": "on", "peer_review": "on", "city": "Chicago",
        "full_client_name": "Globex Corporation", "short_client_name": "Globex",
        "due_date": "2027-03-15"}))
    test.append(write("test_combo_audittax_aboutkpmg_quality", {
        "audit_or_tax": "Audit & Tax", "about_kpmg": "on", "quality": "on",
        "full_client_name": "Initech LLC", "short_client_name": "Initech",
        "due_date": "2027-01-20"}))
    test.append(write("test_combo_expansion_healthcare_symposium", {
        "new_or_expansion": "Expansion of Services", "sector": "Healthcare",
        "sub_sector": "Healthcare", "annual_symposium": "on",
        "full_client_name": "Umbrella Corporation", "short_client_name": "Umbrella",
        "due_date": "2027-06-30"}))

    print("Training payloads: %d" % train_n)
    for n in written:
        print("  " + n + ".json")
    print("\nHeld-out test payloads: %d (excluded from harvesting)" % len(test))
    for n in test:
        print("  " + n + ".json")
    print("\nTotal: %d payloads in %s" % (train_n + len(test), config.PAYLOADS_DIR))


if __name__ == "__main__":
    main()
