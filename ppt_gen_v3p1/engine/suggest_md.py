"""Turn a suggestion report into markdown someone will actually read.

Two shapes, and which one you get is the whole point:

  **first time** - the full walkthrough: deck order, what stays on always,
  every condition to set, what to bind, what to check.

  **already have rules** - only what changed. A second batch of decks should
  surface the two new things, not restate the forty an author has already
  confirmed. Settled rules are counted, not listed; re-reading them to find the
  new ones is how a helper stops being used.
"""


def _val(v):
    """A value as the rules file writes it, not as Python prints it.

    `True` in a report that a `true` in rules.json is what an author has to
    type sends them looking for a difference that is not there.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _cond(when):
    if not when:
        return "always"
    f = when.get("field", "?")
    if "exists" in when:
        return "`%s` is answered" % f
    for op, word in (("eq", "is"), ("ne", "is not")):
        if op in when:
            return "`%s` %s `%s`" % (f, word, _val(when[op]))
    for op, word in (("in", "is one of"), ("not_in", "is not one of")):
        if op in when:
            return "`%s` %s %s" % (f, word,
                                   ", ".join("`%s`" % _val(v) for v in when[op]))
    return "`%s`" % f


def _matching(report):
    out = []
    for d in report["decks"]:
        how = ", ".join("%s %d" % kv for kv in sorted(d["methods"].items())) or "-"
        line = "- `%s` — %d of %d slides matched (%s)" % (
            d["label"], len(d["blocks"]), d["slides"], how)
        if d["unmatched"]:
            line += ("  \n  **%d slide(s) matched nothing in the library.** They "
                     "may be regenerated per deck, or the library may be missing "
                     "them — everything below rests on this step."
                     % d["unmatched"])
        out.append(line)
    return out


def _caveats(report):
    """The ways this evidence can mislead, said once and plainly."""
    out = []
    if len(report["decks"]) == 1:
        out.append("**Only one deck.** Nothing has been seen to vary, so every "
                   "slide in it looks permanent. Add decks that differ before "
                   "trusting any of this.")
    if report["unpaired"]:
        out.append("**No answer set matched %s.** Those decks cannot explain "
                   "anything — only which slides they contain."
                   % ", ".join("`%s`" % l for l in report["unpaired"][:5]))
    if report["fixed_fields"] and report["always"]:
        out.append(
            "**\"In every deck\" means these %d.** %s never varied here, so a "
            "slide one of them controls sits in all of them and looks "
            "permanent. That is the honest reading of this evidence and the "
            "wrong rule for the template — add a deck that answers one "
            "differently."
            % (len(report["labels"]),
               ", ".join("`%s`" % f for f in report["fixed_fields"][:4])))
    return out


def _condition_lines(report, blocks):
    lines = []
    for bid in blocks:
        entry = report["found"].get(bid)
        n = len(report["block_decks"].get(bid, []))
        if not entry:
            lines.append("- `%s` — in %d of %d decks, and **no single answer "
                         "explains it**. Needs a human."
                         % (bid, n, len(report["labels"])))
            continue
        from .suggest import as_condition
        lines.append("- `%s` → %s  *(%d of %d decks)*"
                     % (bid, _cond(as_condition(entry, report["fields"])),
                        n, len(report["labels"])))
        if entry.get("why"):
            lines.append("  - chosen because: %s" % entry["why"])
        alts = report["ambiguous"].get(bid) or []
        for alt in alts[1:4]:
            lines.append("  - ? or `%s` %s" % (
                alt["field"],
                "is answered" if alt["value"] is None else "is `%s`" % alt["value"]))
        if len(alts) > 4:
            lines.append("  - ? …and %d more that fit these decks equally"
                         % (len(alts) - 4))
    return lines


def full(report, placeholders=None):
    """The first-time walkthrough."""
    L = ["# Suggested rules", "",
         "Read this against the screens; nothing here has been applied.", ""]

    L += ["## 1 · Slides", "",
          "Library has **%d** blocks." % len(report["library_blocks"]), ""]
    L += _matching(report) + [""]

    for c in _caveats(report):
        L += ["> " + c, ""]

    L += ["## 2 · Questions", ""]
    if report["fields"]:
        L.append("**%d field(s)** from %d answer set(s)."
                 % (len(report["fields"]), len(report["paired"])))
        if report["fixed_fields"]:
            L.append("")
            L.append("Never vary, so they cannot be a condition: %s"
                     % ", ".join("`%s`" % f for f in report["fixed_fields"]))
    else:
        L.append("No answer sets. Without them this can say which slides vary, "
                 "but not why.")
    L.append("")

    L += ["## 3 · Rules", "", "**Deck order** — drag the rows until they read:", ""]
    L += ["%d. `%s`%s" % (i, b, "" if b in report["block_decks"] else "  ← in none of these decks")
          for i, b in enumerate(report["consensus"], 1)]
    L.append("")
    if report["never"]:
        L += ["Drag **out** of the deck: %s"
              % ", ".join("`%s`" % b for b in report["never"]), ""]
    L += ["**Leave on `always`** (%d): %s"
          % (len(report["always"]),
             ", ".join("`%s`" % b for b in report["always"]) or "none"), ""]
    L += ["**Give these a condition** (%d):" % len(report["varies"]), ""]
    L += _condition_lines(report, report["varies"]) or ["- none"]
    L.append("")

    if placeholders:
        L += ["## 4 · Values", ""]
        for p in sorted(placeholders):
            guess = _guess(p, report["fields"])
            L.append("- `{{%s}}` → %s" % (
                p, ("field `%s` *(%s — confirm it)*" % guess) if guess
                else "**no matching answer field** — a literal, or an external source"))
        L += ["", "Anything left unbound prints as `{{...}}` in the deck.", ""]

    L += ["## 5 · Check", "",
          "Build with each answer set and compare against its deck. The goal is "
          "`MATCH`; anything else names the slide to fix.", ""]
    return "\n".join(L)


def _guess(placeholder, fields):
    if placeholder in fields:
        return placeholder, "exact"
    low = placeholder.lower()
    near = [f for f in fields if low in f.lower() or f.lower() in low]
    return (near[0], "similar name") if len(near) == 1 else None


def changes(report, diff):
    """Only what these decks change about the rules already saved."""
    L = ["# What these decks change", "",
         "Compared against the rules already saved for this library. "
         "**%d rule(s) already match** and are not repeated below."
         % len(diff["settled"]), ""]

    L += _matching(report) + [""]
    for c in _caveats(report):
        L += ["> " + c, ""]

    if diff["nothing_to_do"]:
        L += ["## Nothing to change", "",
              "Every slide these decks contain is already ruled the way the "
              "evidence suggests. If you expected a change, check that the new "
              "answer sets were uploaded on the Questions screen — a deck whose "
              "payload is missing can only say which slides it holds.", ""]
        return "\n".join(L)

    if diff["new"]:
        L += ["## New — set these", ""]
        for item in diff["new"]:
            L.append("- `%s` → %s" % (item["block"], _cond(item["suggested"])))
            L.append("  - %s" % item["reason"])
            for alt in (report["ambiguous"].get(item["block"]) or [])[1:3]:
                L.append("  - ? or `%s` %s" % (
                    alt["field"], "is answered" if alt["value"] is None
                    else "is `%s`" % alt["value"]))
        L.append("")

    if diff["changed"]:
        L += ["## Changed — the decks disagree with what you have", ""]
        for item in diff["changed"]:
            L.append("- `%s`" % item["block"])
            L.append("  - you have: %s" % _cond(item["mine"]))
            L.append("  - these decks say: %s"
                     % (_cond(item["suggested"]) if item["suggested"]
                        else "**no single answer explains it**"))
            L.append("  - %s" % item["reason"])
        L += ["", "> Your rule may still be right. A batch that does not "
                  "exercise a field cannot confirm the rule that uses it.", ""]

    if diff["not_in_deck"]:
        L += ["## In these decks but not in yours", "",
              "%s — the decks contain them and your rules do not."
              % ", ".join("`%s`" % b for b in diff["not_in_deck"]), ""]

    if diff["questions"]:
        L += ["## Worth a look", ""]
        for item in diff["questions"]:
            L.append("- `%s` has %s, but is in **every** one of these decks — "
                     "this batch does not test that rule."
                     % (item["block"], _cond(item["mine"])))
        L.append("")
    return "\n".join(L)
