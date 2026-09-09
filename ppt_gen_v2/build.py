#!/usr/bin/env python3
"""Build proposal decks from owned templates.

    python build.py import  <deck.pptx> --name office     # add a template
    python build.py list                                  # what's registered
    python build.py inspect office                        # blocks, ids, rules check
    python build.py rename  office slide_7 executive_summary
    python build.py preview office --out out/sheet.html    # slide images (built in)
    python build.py check   office                        # write report.md
    python build.py reconcile office                      # re-key blocks.json
    python build.py tokenise  office --payload p.json     # put {{placeholders}} back
    python build.py rules     office --payloads data/payloads  # re-propose rules
    python build.py bind      office "{{X}}" --field X    # bind a placeholder
    python build.py verify    office --decks d --payloads p   # match Templafy?
    python build.py make    office --answers a.json --out out/deck.pptx

Adding a template is a file operation — drop a folder under templates/, or run
`import`. No code changes, ever.

Every build is validated before it is reported as done (decision D5): a deck
that does not pass never reaches a user.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from engine import (Template, TemplateError, build_template,  # noqa: E402
                    find_template, import_deck, list_templates)
from engine import report as reportmod                         # noqa: E402
from engine import svg as svgmod                               # noqa: E402
from engine.assemble import AssemblyError                     # noqa: E402
from engine.library import LibraryError                       # noqa: E402
from engine.rules import Rules, RulesError                    # noqa: E402

TEMPLATES = os.path.join(HERE, "templates")


def load_validator():
    """The structural validator - vendored, so it is always present.

    It used to be imported from v1 next door. On a standalone deployment that
    import found nothing and every build ran unvalidated while reporting
    success, which is exactly what decision D5 exists to prevent.
    """
    from engine import validate as _validate
    return _validate


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve(args):
    root = args.templates or TEMPLATES
    return find_template(root, args.template)


# ---------------------------------------------------------------- list
def cmd_list(args):
    templates = list_templates(args.templates or TEMPLATES)
    if not templates:
        print("No templates in %s\n\nAdd one:\n  python build.py import "
              "<your-deck.pptx> --name <name>" % (args.templates or TEMPLATES))
        return 0
    print("%-16s %-8s %-8s %s" % ("TEMPLATE", "BLOCKS", "UNNAMED", "DESCRIPTION"))
    for t in templates:
        d = t.describe()
        if "error" in d:
            print("%-16s %s" % (d["name"], "! " + d["error"]))
            continue
        print("%-16s %-8d %-8s %s"
              % (d["name"], d["blocks"], d["unnamed"] or "-",
                 d["description"] or ""))
        if d["answer_sets"]:
            print("%-16s   answers: %s" % ("", ", ".join(d["answer_sets"])))
    return 0


# ---------------------------------------------------------------- import
def cmd_import(args):
    report = import_deck(args.deck, args.templates or TEMPLATES, name=args.name,
                         description=args.description or "",
                         overwrite=args.overwrite)
    print("Imported %s as template %r" % (os.path.basename(args.deck), report["name"]))
    print("  %s" % report["folder"])
    print("  %d slides -> %d blocks (%d from markers, %d from slide titles, "
          "%d positional)"
          % (report["slides"], report["slides"], report["ids_from_markers"],
             report["ids_from_titles"], report["ids_from_position"]))
    if report["placeholders"]:
        print("  placeholders found: %s"
              % ", ".join("{{%s}}" % p for p in report["placeholders"]))
    else:
        print("  no {{placeholders}} in the deck yet — add them in PowerPoint "
              "where values belong")

    print("\nWrote a starter rules.json: every slide, in the deck's own order.")
    print("It builds as-is. Next:")
    print("  1. python build.py inspect %s        # check the block ids read well"
          % report["name"])
    print("  2. edit %s" % os.path.join(report["folder"], "blocks.json"))
    print("     (or: python build.py rename %s <old> <new>)" % report["name"])
    print("  3. edit %s" % os.path.join(report["folder"], "rules.json"))
    print("     move optional blocks out of `baseline` and give them a `when`")
    return 0


# ---------------------------------------------------------------- inspect
def cmd_inspect(args):
    tpl = resolve(args)
    with tpl.open_library() as lib:
        print("Template: %s" % tpl.name)
        print("Library:  %s (%d blocks)\n"
              % (os.path.basename(tpl.library_path), len(lib.blocks)))
        rows = lib.summary()
        width = max([len(b["id"]) for b in rows] + [8])
        fmt = "  %-2s %-" + str(width) + "s %-8s %s"
        print(fmt % ("", "BLOCK ID", "FROM", "PLACEHOLDERS"))
        for b in rows:
            flag = " " if b["marked"] else "?"
            print(fmt % (flag, b["id"], b["source"],
                         ", ".join("{{%s}}" % p for p in b["placeholders"]) or "-"))

        loose = [b for b in lib.summary() if not b["marked"]]
        if loose:
            print("\n  ? %d block(s) have no stable id — they are named by slide "
                  "title or position," % len(loose))
            print("    so reordering the deck renames them. Fix with "
                  "`rename`, or add a {{block:id}} marker.")

        for line in lib.map_drift():
            print("  ! %s" % line)

        if not os.path.exists(tpl.rules_path):
            print("\nNo rules.json yet.")
            return 0

        rules = Rules.load(tpl.rules_path)
        problems = rules.check_against(lib)
        bound = {p.strip("{} ") for p in rules.placeholders}
        missing = sorted(lib.all_placeholders() - bound)
        unused = sorted(bound - lib.all_placeholders())
        print("\nRules: %s" % os.path.basename(tpl.rules_path))
        for p in problems:
            print("  ! %s" % p)
        if missing:
            print("  ! in the library but not bound: %s"
                  % ", ".join("{{%s}}" % m for m in missing))
        if unused:
            print("  - bound but not used by any slide: %s"
                  % ", ".join("{{%s}}" % m for m in unused))
        if not (problems or missing):
            print("  OK - every referenced block exists and every placeholder "
                  "is bound")
    return 0


# ---------------------------------------------------------------- rename
def cmd_rename(args):
    tpl = resolve(args)
    part = tpl.rename_block(args.old, args.new)
    print("Renamed %s -> %s (%s); rules.json updated" % (args.old, args.new, part))
    return 0


# ---------------------------------------------------------------- tokenise
def cmd_tokenise(args):
    """Restore placeholders in a library whose values are already filled in.

    A library merged from generated decks is a snapshot of one client: the
    cover says "Example Corporation", not "{{ClientName}}". This puts the
    placeholders back on the library you already have, rather than rebuilding
    it - which matters once PowerPoint has agreed to open it.
    """
    from engine import tokenise as tk

    tpl = resolve(args)
    payload = read_json(args.payload)
    report = tk.tokenise_library(tpl, payload, backup=not args.no_backup)

    hits = report["replacements"]
    if not hits:
        print("Nothing replaced - none of the payload's values appear in the")
        print("deck text as written. Check you passed the payload that")
        print("generated these slides.")
        return 1

    print("Restored placeholders in %d slide(s):" % report["slides_changed"])
    for literal, n in sorted(hits.items(), key=lambda kv: -kv[1]):
        print("   %-36s %3d occurrence(s)" % ('"%s"' % literal[:34], n))
    if report["backup"]:
        print("   previous library kept at %s" % os.path.basename(report["backup"]))

    # bind them, or the placeholders are visible text and nothing fills them
    rules = read_json(tpl.rules_path) if os.path.exists(tpl.rules_path) else {}
    rules.setdefault("placeholders", {})
    rules["placeholders"].update(tk.bindings_for(report["map"], report["formats"]))
    with open(tpl.rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
    print("   bound %d placeholder(s) in rules.json" % len(report["map"]))

    print("")
    print("Check them: a value can be a real answer in one place and ordinary")
    print("wording in another (v1 hit this with \"New York\").")
    print("   python build.py check   %s" % tpl.name)
    print("   python build.py preview %s" % tpl.name)
    return 0


# ---------------------------------------------------------------- bind
def cmd_bind(args):
    """Point a placeholder at the answer that fills it."""
    tpl = resolve(args)
    name = args.placeholder.strip()
    if not name.startswith("{{"):
        name = "{{%s}}" % name.strip("{} ")
    rules = read_json(tpl.rules_path) if os.path.exists(tpl.rules_path) else {}
    binding = {"from": "field", "field": args.field}
    if args.format:
        binding["format"] = args.format
    rules.setdefault("placeholders", {})[name] = binding
    with open(tpl.rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2, ensure_ascii=False)
    print("Bound %s -> answers.%s%s"
          % (name, args.field, " (%s)" % args.format if args.format else ""))

    with tpl.open_library() as lib:
        if name.strip("{} ") not in lib.all_placeholders():
            print("  ! no slide uses %s - check the spelling" % name)
    return 0


# ---------------------------------------------------------------- verify
def cmd_verify(args):
    """Compare our decks against the ones Templafy produced.

    Everything else checks a deck is valid. This checks it is *right* - same
    slides, same order, for the same answers - against the only authority
    there is.
    """
    from engine import verify as verifymod

    tpl = resolve(args)
    report = verifymod.verify(tpl, args.decks, args.payloads, limit=args.limit)
    if not report["compared"]:
        print("Nothing to compare - no deck could be paired with a payload.")
        return 2

    for row in report["results"]:
        mark = "ok  " if row.get("exact") else (
            "sel " if row.get("selection_ok") else "FAIL")
        detail = row.get("error") or ("%d vs %d slides, %d matched"
                                      % (row.get("our_slides", 0),
                                         row.get("templafy_slides", 0),
                                         row.get("matched", 0)))
        print("  %s %-26s %-34s %s"
              % (mark, row["deck"], row["verdict"], detail))
        if args.verbose:
            for s in row.get("missing", [])[:5]:
                print("         missing at %-3s %s" % (s["index"], s["preview"]))
            for s in row.get("extra", [])[:5]:
                print("         extra   at %-3s %s" % (s["index"], s["preview"]))
            for t in row.get("text_differs", [])[:3]:
                for seg in t["segments"][:2]:
                    print("         text: ours %r vs templafy %r"
                          % (seg["ours"][:40], seg["templafy"][:40]))

    print("")
    print("%d of %d deck(s) match exactly; %d have the right slides in the "
          "right order" % (report["exact"], report["compared"],
                           report["selection_ok"]))
    if report["unpaired_decks"]:
        print("  ! %d deck(s) had no payload: %s"
              % (len(report["unpaired_decks"]),
                 ", ".join(report["unpaired_decks"][:5])))
    print("")
    print("Slides are matched by creationId, then by layout and geometry, then")
    print("by text - whichever is strongest. A deck Templafy regenerates per")
    print("client (fees, partners) cannot be matched by identity and is counted")
    print("separately rather than as an error.")
    return 0 if report["selection_ok"] == report["compared"] else 1


# ---------------------------------------------------------------- rules
def cmd_rules(args):
    """Re-derive the rule proposals from provenance.json, without re-merging.

    A merged template records which decks each slide came from. Paired with the
    payloads that produced those decks, that says *why* each slide was there -
    which is the rules. Rebuilding them from that evidence avoids re-running
    the merge and risking a library PowerPoint is finally happy with.
    """
    from engine import propose

    tpl = resolve(args)
    report = propose.propose_from_provenance(tpl, args.payloads,
                                             decks_dir=args.decks)

    print("Rewrote %s" % os.path.basename(tpl.rules_path))
    print("  %d baseline (in every deck), %d conditional (proposed)"
          % (report["baseline"], report["conditional"]))
    print("  %d placeholder binding(s) kept" % report["placeholders_kept"])
    if report["anchors_from_decks"]:
        print("  %d block(s) placed from the decks' own slide order"
              % report["anchors_from_decks"])
    if report["anchors_guessed"]:
        print("  ! %d block(s) placed from library order, not from the decks -"
              % report["anchors_guessed"])
        print("    their position is a guess, marked _confirm_position. Pass")
        print("    --decks <folder> to recover the real order from the decks.")
    m = report.get("deck_matching")
    if m and m["unmatched"]:
        print("  ! %d of %d slides across %d deck(s) could not be matched to a"
              % (m["unmatched"], m["matched"] + m["unmatched"], m["decks"]))
        print("    block, so their positions stay guesses. Slides are matched")
        print("    by creationId; geometry is used only when it names one block.")
    for line in report.get("unreadable_decks") or []:
        print("  ! could not read %s" % line)
    if report["unpaired_decks"]:
        print("  ! no payload matched %d deck(s): %s"
              % (len(report["unpaired_decks"]),
                 ", ".join(report["unpaired_decks"][:5])))
    if report["stale_blocks"]:
        print("  ! %d block(s) in provenance are not in the library: %s"
              % (len(report["stale_blocks"]), ", ".join(report["stale_blocks"][:5])))
    for bid in report["unanchored"]:
        print("  ? %s has no stable block before it - it will be appended" % bid)
    for note in report["notes"]:
        print("  ? %s" % note)
    print("")
    print("These are proposals. Confirm them before building for a client:")
    print("  python build.py check   %s" % tpl.name)
    print("  python build.py preview %s" % tpl.name)
    return 0


# ---------------------------------------------------------------- reconcile
def cmd_reconcile(args):
    """Re-key blocks.json after the library deck has been rewritten.

    The sidecar maps slide part names to block ids, so anything that renumbers
    parts - a designer reordering slides, or PowerPoint rewriting the package
    during a repair - leaves the ids naming the wrong slides. Nothing errors;
    the rules just quietly pull the wrong content.
    """
    tpl = resolve(args)
    report = tpl.reconcile_blocks()
    if not report["changed"] and not report["unmatched"] and not report["vanished"]:
        print("blocks.json already matches the library - nothing to do")
        return 0
    for bid, was, now in report["moved"]:
        print("  moved     %-30s %s -> %s" % (bid, was, now))
    for bid, was in report["unmatched"]:
        print("  ! LOST    %-30s was %s - no slide with its recorded title"
              % (bid, was))
    for part in report["vanished"]:
        print("  ! UNNAMED %s - a slide no block id points at" % part)
    if report["changed"]:
        print("")
        print("Rewrote %s" % tpl.blocks_path)
    if report["unmatched"] or report["vanished"]:
        print("")
        print("Check these by eye before building - a block id on the wrong")
        print("slide produces a deck that looks right and is not:")
        print("  python build.py preview %s" % tpl.name)
        return 1
    return 0


# ---------------------------------------------------------------- check
def cmd_check(args):
    """Inspect a template and write report.md.

    Exists because templates live on machines we cannot see, holding decks
    nobody outside the firm may look at. When something is wrong, the thing to
    hand over is a description of the problem, not the deck.
    """
    tpl = resolve(args)
    answers = read_json(args.answers) if args.answers else None
    rep = reportmod.check(tpl, answers=answers, redact=args.redact,
                          validator=load_validator(), sample=not args.no_build)

    out = args.out or os.path.join(tpl.folder, "report.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(reportmod.to_markdown(rep, include_blocks=not args.brief))

    errors, warnings = rep.of(reportmod.ERROR), rep.of(reportmod.WARNING)
    print("Wrote %s" % out)
    print("  %d error(s), %d warning(s), %d note(s)"
          % (len(errors), len(warnings), len(rep.of(reportmod.NOTE))))
    for finding in (errors + warnings)[:12]:
        print("  %-8s %s" % (finding.level.upper(), finding.message))
    if len(errors) + len(warnings) > 12:
        print("  ... %d more in the report"
              % (len(errors) + len(warnings) - 12))
    if not args.redact:
        print("  Note: the report quotes slide titles. Re-run with --redact "
              "before sharing it outside the firm.")
    return 1 if errors else 0


# ---------------------------------------------------------------- preview
def cmd_preview(args):
    """Render every block to SVG, as a contact sheet you can look at.

    Thumbnails are the one thing no test can sign off — a slide can be
    structurally perfect and still look wrong — so the output is a page for a
    human. Rendering is in-process: nothing to install.
    """
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from make_contact_sheet import build_page

    tpl = resolve(args)
    results = svgmod.render_template(tpl, block_ids=args.blocks or None)
    out = args.out or os.path.join(HERE, "out", "%s_contact_sheet.html" % tpl.name)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(build_page(tpl, results))

    failed = [r for r in results if r.get("error")]
    unsupported = sorted({u for r in results for u in (r.get("unsupported") or [])})
    print("Wrote %s - %d block(s)" % (out, len(results)))
    if unsupported:
        print("  drawn as labelled boxes (not renderable): %s"
              % ", ".join(unsupported))
    for r in failed:
        print("  ! %s did not render: %s" % (r["id"], r["error"]))
    return 1 if failed else 0


# ---------------------------------------------------------------- make
def cmd_make(args):
    tpl = resolve(args)
    answers_path = args.answers
    if not answers_path:
        sets = tpl.answer_sets()
        if len(sets) != 1:
            raise TemplateError(
                "--answers is required (template %r has %s)"
                % (tpl.name, ", ".join(sets) if sets else "no answers*.json"))
        answers_path = os.path.join(tpl.folder, sets[0])
        print("Using %s" % sets[0])

    out = args.out or os.path.join(
        HERE, "out", "%s_%s.pptx"
        % (tpl.name, os.path.splitext(os.path.basename(answers_path))[0]))

    report = build_template(tpl, read_json(answers_path), out, strict=args.strict)

    print("Built %s - %d slides" % (report["out"], report["slides"]))
    if args.verbose:
        for line in report["trace"]:
            print("  rule: %s" % line)
        for i, s in enumerate(report["order"], 1):
            print("  %2d. %-26s (%s)" % (i, s["block"], s["source"]))
    if report["unresolved_bindings"]:
        print("  ! unresolved bindings (data sources not wired yet):")
        for u in report["unresolved_bindings"]:
            print("      %s" % u)
    if report["unfilled_placeholders"]:
        print("  ! left on the slides unfilled: %s"
              % ", ".join("{{%s}}" % p for p in report["unfilled_placeholders"]))

    validator = load_validator()
    if validator is None:
        print("  ! validate_pptx.py not found - output NOT validated")
        return 0
    issues = validator.validate(report["out"])
    if issues:
        print("  FAILED validation - %d issue(s):" % len(issues))
        for kind, where, detail in issues[:20]:
            print("      [%s] %s: %s" % (kind, where, detail))
        return 1
    print("  validated OK - no repair dialog expected")
    return 0


# ---------------------------------------------------------------- cli
def main(argv=None):
    p = argparse.ArgumentParser(
        prog="build.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--templates", help="template root (default: ./templates)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list registered templates")

    imp = sub.add_parser("import", help="turn a .pptx into a template folder")
    imp.add_argument("deck")
    imp.add_argument("--name", help="template id (default: from the file name)")
    imp.add_argument("--description")
    imp.add_argument("--overwrite", action="store_true")

    ins = sub.add_parser("inspect", help="blocks, ids, and a rules check")
    ins.add_argument("template")

    ren = sub.add_parser("rename", help="rename a block, updating rules.json")
    ren.add_argument("template")
    ren.add_argument("old")
    ren.add_argument("new")

    pv = sub.add_parser("preview", help="render blocks to an SVG contact sheet")
    pv.add_argument("template")
    pv.add_argument("--out", help="output .html")
    pv.add_argument("--blocks", nargs="*", help="only these blocks")

    tk = sub.add_parser("tokenise", aliases=["tokenize"],
                        help="put {{placeholders}} back into a filled-in library")
    tk.add_argument("template")
    tk.add_argument("--payload", required=True,
                    help="the payload whose values are baked into these slides")
    tk.add_argument("--no-backup", action="store_true")

    bd = sub.add_parser("bind", help="bind a placeholder to an answer field")
    bd.add_argument("template")
    bd.add_argument("placeholder")
    bd.add_argument("--field", required=True)
    bd.add_argument("--format", help="e.g. long_comma for dates")

    vf = sub.add_parser("verify",
                        help="compare our decks with the ones Templafy produced")
    vf.add_argument("template")
    vf.add_argument("--decks", required=True)
    vf.add_argument("--payloads", required=True)
    vf.add_argument("--limit", type=int, help="only the first N payloads")
    vf.add_argument("-v", "--verbose", action="store_true")

    ru = sub.add_parser("rules",
                        help="re-propose rules from provenance.json + payloads")
    ru.add_argument("template")
    ru.add_argument("--payloads", required=True,
                    help="folder of the payloads that generated the decks")
    ru.add_argument("--decks",
                    help="folder of the source decks, to recover the real slide "
                         "order (only the decks know it)")

    rc = sub.add_parser("reconcile",
                        help="re-key blocks.json after the library was rewritten")
    rc.add_argument("template")

    ck = sub.add_parser("check", help="write report.md describing any problems")
    ck.add_argument("template")
    ck.add_argument("--out", help="output .md (default: <template>/report.md)")
    ck.add_argument("--redact", action="store_true",
                    help="drop slide titles and quoted text, keep every finding")
    ck.add_argument("--answers", help="answers .json for the test build")
    ck.add_argument("--no-build", action="store_true",
                    help="skip the test build")
    ck.add_argument("--brief", action="store_true",
                    help="omit the per-block table")

    mk = sub.add_parser("make", help="build a deck")
    mk.add_argument("template")
    mk.add_argument("--answers")
    mk.add_argument("--out")
    mk.add_argument("--strict", action="store_true",
                    help="fail if any placeholder is left unfilled")
    mk.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0

    handler = {"list": cmd_list, "import": cmd_import, "inspect": cmd_inspect,
               "rename": cmd_rename, "make": cmd_make,
               "preview": cmd_preview, "check": cmd_check,
               "reconcile": cmd_reconcile, "tokenise": cmd_tokenise,
               "tokenize": cmd_tokenise, "rules": cmd_rules,
               "bind": cmd_bind, "verify": cmd_verify}[args.cmd]
    try:
        return handler(args)
    except (AssemblyError, RulesError, LibraryError, TemplateError) as exc:
        print("%s failed: %s" % (args.cmd, exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
