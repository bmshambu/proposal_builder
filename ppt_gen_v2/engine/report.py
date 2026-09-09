"""Health check for a template, written out as `report.md`.

Templates live on machines this code's authors cannot see, holding decks nobody
outside the firm may look at. So when something is wrong, the useful thing to
hand over is not the deck — it is a description of what is wrong with it.

`check()` gathers findings; `to_markdown()` renders them. They are kept apart so
the author console can show the same findings without going through Markdown.

Findings carry a level:

    error    the template will not build, or builds something broken
    warning  it works, but something will bite later (unstable ids, drift)
    note     worth knowing; no action implied

**On confidentiality:** the report names slides and can quote their titles,
which in a proposal template is client-identifying. `check(redact=True)` keeps
every count, id and structural finding but drops the quoted text, so a report
can be shared outside the firm. The rendered document says which mode produced
it, at the top, every time.
"""
import os
import zipfile

from . import svg
from .library import LibraryError
from .rules import Rules, RulesError

ERROR, WARNING, NOTE = "error", "warning", "note"
_ORDER = {ERROR: 0, WARNING: 1, NOTE: 2}

REDACTED = "[redacted]"


class Finding:
    __slots__ = ("level", "code", "message", "detail", "where")

    def __init__(self, level, code, message, detail=None, where=None):
        self.level, self.code = level, code
        self.message, self.detail, self.where = message, detail, where

    def as_dict(self):
        return {"level": self.level, "code": self.code, "message": self.message,
                "detail": self.detail, "where": self.where}


class Report:
    def __init__(self, template):
        self.template = template
        self.findings = []
        self.facts = {}
        self.blocks = []

    def add(self, level, code, message, detail=None, where=None):
        self.findings.append(Finding(level, code, message, detail, where))

    def of(self, level):
        return [f for f in self.findings if f.level == level]

    @property
    def ok(self):
        return not self.of(ERROR)

    def sorted_findings(self):
        return sorted(self.findings, key=lambda f: (_ORDER[f.level], f.code))


# ---------------------------------------------------------------- checks
def check(template, answers=None, redact=False, validator=None, sample=True):
    """Inspect a template end to end. -> Report"""
    rep = Report(template)
    keep = (lambda text: REDACTED) if redact else (lambda text: text)
    rep.facts["redacted"] = redact
    rep.facts["template"] = template.name
    rep.facts["folder"] = template.folder

    # -- the library opens at all -------------------------------------
    try:
        lib = template.open_library()
    except LibraryError as exc:
        rep.add(ERROR, "library-unreadable",
                "The library deck could not be read", str(exc))
        return rep

    with lib:
        _check_package(rep, template, lib, validator)
        _check_blocks(rep, lib, keep)
        rules = _check_rules(rep, template, lib)
        _check_render(rep, template)

    _check_provenance(rep, template, keep)
    if sample and rules is not None:
        _check_sample_build(rep, template, answers, validator)
    return rep


def _check_package(rep, template, lib, validator):
    path = template.library_path
    size = os.path.getsize(path)
    rep.facts["library"] = os.path.basename(path)
    rep.facts["library_bytes"] = size
    rep.facts["slides"] = len(lib.slide_parts)

    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    rep.facts["masters"] = sum(1 for n in names if "slideMasters/slideMaster" in n
                               and n.endswith(".xml"))
    rep.facts["layouts"] = sum(1 for n in names if "slideLayouts/slideLayout" in n
                               and n.endswith(".xml"))
    rep.facts["media"] = sum(1 for n in names if n.startswith("ppt/media/"))

    if validator is not None:
        issues = validator.validate(path)
        rep.facts["validator_issues"] = len(issues)
        for kind, where, detail in issues:
            rep.add(ERROR, "invalid-package",
                    "PowerPoint would offer to repair this deck: %s" % kind,
                    detail, where)
        if not issues:
            rep.add(NOTE, "package-valid",
                    "The library package validates - no repair dialog expected")
    else:
        rep.add(WARNING, "no-validator",
                "validate_pptx.py was not found, so the package was not "
                "structurally checked")

    # A real branded template legitimately has a handful of masters (title,
    # content, section, appendix). Dozens means a merge failed to de-duplicate
    # them - which is the thing worth saying.
    if rep.facts["masters"] > 8:
        rep.add(WARNING, "many-masters",
                "%d slide masters in one library" % rep.facts["masters"],
                "Decks merged from one template should end up sharing a handful "
                "of masters. This many means the source decks' masters were not "
                "recognised as the same - the library will be large and may look "
                "inconsistent.")


def _check_blocks(rep, lib, keep):
    by_source = {}
    for block in lib.ordered():
        by_source[block.source] = by_source.get(block.source, 0) + 1
        rep.blocks.append({
            "id": block.id, "slide": os.path.basename(block.part),
            "source": block.source, "title": keep(block.title),
            "placeholders": sorted(block.placeholders),
        })
    rep.facts["blocks"] = len(lib.blocks)
    rep.facts["id_sources"] = by_source

    loose = [b.id for b in lib.ordered() if not b.marked]
    if loose:
        rep.add(WARNING, "unstable-ids",
                "%d block(s) are named by slide title or position" % len(loose),
                "Reordering the deck renames them, which silently breaks every "
                "rule that refers to them. Fix with `build.py rename`, or add a "
                "{{block:id}} marker.\n\nAffected: " + ", ".join(loose[:25]))

    drift = lib.map_drift()
    for line in drift:
        rep.add(WARNING, "sidecar-drift",
                "A slide changed since its id was recorded", keep(line))
    if drift:
        # The sidecar is keyed by part name, so a deck someone reordered - or
        # that PowerPoint rewrote during a repair - leaves ids on the wrong
        # slides. Nothing is invalid, so nothing else will say so.
        rep.add(WARNING, "sidecar-needs-reconcile",
                "%d slide(s) no longer match the sidecar" % len(drift),
                "If the deck was reordered or repaired, the block ids are on "
                "the wrong slides and every rule will pull the wrong content."
                "\n\nRe-key it, then look at the result:\n\n"
                "    python build.py reconcile <template>\n"
                "    python build.py preview <template>")

    empty = [b.id for b in lib.ordered() if not b.title]
    if empty:
        rep.add(NOTE, "untitled-slides",
                "%d slide(s) have no readable title" % len(empty),
                "Their ids came from position. Affected: " + ", ".join(empty[:25]))


def _check_rules(rep, template, lib):
    if not os.path.exists(template.rules_path):
        rep.add(ERROR, "no-rules",
                "The template has no rules.json, so no deck can be built")
        return None
    try:
        rules = Rules.load(template.rules_path)
    except (RulesError, ValueError) as exc:
        rep.add(ERROR, "rules-unreadable", "rules.json could not be read", str(exc))
        return None

    for problem in rules.check_against(lib):
        rep.add(ERROR, "rules-mismatch",
                "The rules refer to something the library does not have", problem)

    bound = {p.strip("{} ") for p in rules.placeholders}
    used = lib.all_placeholders()
    missing, unused = sorted(used - bound), sorted(bound - used)
    rep.facts["placeholders_used"] = len(used)
    rep.facts["placeholders_bound"] = len(bound)

    if missing:
        rep.add(ERROR, "unbound-placeholder",
                "%d placeholder(s) appear on slides but nothing fills them"
                % len(missing),
                "They will be left visible in every deck built from this "
                "template.\n\n" + "\n".join("- `{{%s}}`" % m for m in missing))
    if unused:
        rep.add(NOTE, "unused-binding",
                "%d binding(s) are declared but no slide uses them" % len(unused),
                ", ".join("`{{%s}}`" % m for m in unused))
    if not rules.baseline:
        rep.add(ERROR, "empty-baseline",
                "The baseline is empty, so a default answer set produces no deck")

    # A template with nothing to substitute produces the same deck for every
    # client. When the library was merged from *generated* decks this is the
    # expected failure: the values were already substituted before the merge,
    # so what looks like a template is a snapshot of whoever it was built for.
    if not used:
        merged = os.path.exists(os.path.join(template.folder, "provenance.json"))
        rep.add(ERROR if merged else WARNING, "no-placeholders",
                "No slide contains a placeholder, so every deck this template "
                "builds will be identical",
                ("This library was merged from generated decks, whose values "
                 "were already substituted - so it is a snapshot of one "
                 "client, not a template. Re-run the merge with `--tokenise` "
                 "to put `{{ClientName}}`, `{{DueDate}}` and `{{City}}` back "
                 "where that client's values appear."
                 if merged else
                 "Add `{{Placeholders}}` in PowerPoint where values belong, "
                 "then bind them in rules.json."))

    unconfirmed = [b for b, spec in rules.blocks.items()
                   if isinstance(spec, dict) and "_confirm" in spec]
    if unconfirmed:
        rep.add(WARNING, "unconfirmed-rule",
                "%d proposed rule(s) still need a human decision" % len(unconfirmed),
                "More than one answer explained these equally well when the "
                "decks were merged.\n\n"
                + "\n".join("- `%s`: %s" % (b, rules.blocks[b]["_confirm"])
                            for b in unconfirmed[:20]))

    unanchored = [b for b, spec in rules.blocks.items()
                  if isinstance(spec, dict) and b not in rules.baseline
                  and not spec.get("insert_after")]
    if unanchored:
        rep.add(WARNING, "unanchored-block",
                "%d conditional block(s) have no insert_after" % len(unanchored),
                "They are appended to the end of the deck rather than placed.\n\n"
                + ", ".join("`%s`" % b for b in unanchored[:25]))
    return rules


def _check_render(rep, template):
    """Previews are how an author recognises a slide; a block that will not
    draw is a hole in the console, not just a cosmetic problem."""
    try:
        results = svg.render_template(template)
    except Exception as exc:                      # pragma: no cover - defensive
        rep.add(WARNING, "preview-failed",
                "The preview renderer could not run",
                "%s: %s" % (type(exc).__name__, exc))
        return
    broken = [r for r in results if r.get("error")]
    for r in broken:
        rep.add(WARNING, "block-will-not-render",
                "Block `%s` cannot be drawn in the preview" % r["id"],
                r["error"], r.get("slide"))
    unsupported = {}
    for r in results:
        for kind in (r.get("unsupported") or []):
            unsupported.setdefault(kind, []).append(r["id"])
    for kind, blocks in sorted(unsupported.items()):
        rep.add(NOTE, "preview-approximated",
                "%d block(s) contain a %s, shown as a labelled box"
                % (len(blocks), kind),
                "The slide itself is intact and builds normally - only the "
                "preview approximates it.\n\n"
                + ", ".join("`%s`" % b for b in blocks[:25]))
    rep.facts["blocks_rendered"] = len(results) - len(broken)


def _check_provenance(rep, template, keep):
    """When the library came from merged decks, the merge's own evidence is
    the most useful thing in the report."""
    path = os.path.join(template.folder, "provenance.json")
    if not os.path.exists(path):
        return
    import json
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        rep.add(WARNING, "provenance-unreadable",
                "provenance.json exists but could not be read")
        return

    blocks = data.get("blocks") or {}
    rep.facts["merged_from_decks"] = data.get("deck_count")
    methods = {}
    for info in blocks.values():
        how = info.get("identified_by", "?")
        methods[how] = methods.get(how, 0) + 1
    rep.facts["identified_by"] = methods

    if methods and "creationId" not in methods:
        rep.add(WARNING, "weak-slide-identity",
                "No slide was matched by creationId when the decks were merged",
                "Slides were matched on structure and text instead. Two decks "
                "whose only difference is the client name may have produced two "
                "copies of the same slide. Check for near-duplicate blocks.")

    total = data.get("deck_count") or 0
    if total and blocks:
        singletons = [b for b, i in blocks.items() if i.get("deck_count") == 1]
        share = len(singletons) / float(len(blocks))
        if total > 3 and share > 0.9:
            # Decks from one template share most of their slides. If almost
            # every block came from exactly one deck, nothing matched across
            # them - the merge did not merge, and the library is every deck
            # stacked end to end rather than a template.
            rep.add(ERROR, "merge-did-not-merge",
                    "%d of %d blocks came from exactly one deck - the decks did "
                    "not de-duplicate" % (len(singletons), len(blocks)),
                    "Decks generated from one template share most of their "
                    "slides, so this means no two decks' copies of a slide were "
                    "recognised as the same slide. The library is the decks "
                    "stacked end to end, not a template.\n\n"
                    "Run `python tools/build_master_deck.py <decks> --probe` to "
                    "see how slides are being identified and why.")
        elif len(singletons) > max(3, total // 2):
            rep.add(NOTE, "many-singletons",
                    "%d block(s) appeared in only one deck" % len(singletons),
                    "Expected if each payload turns on its own section; worth a "
                    "glance if not.")


def _check_sample_build(rep, template, answers, validator):
    """Build a real deck. Every other check can pass and this still fail."""
    import json
    import tempfile
    from .assemble import AssemblyError, build_template

    if answers is None:
        sets = template.answer_sets()
        if not sets:
            rep.add(NOTE, "no-sample-answers",
                    "No answers*.json in the template, so no test build was run",
                    "Add one, or pass --answers, to have the check build a deck.")
            return
        with open(os.path.join(template.folder, sets[0]), encoding="utf-8") as fh:
            answers = json.load(fh)
        rep.facts["sample_answers"] = sets[0]

    tmp = tempfile.mkdtemp(prefix="pptgen2_check_")
    out = os.path.join(tmp, "sample.pptx")
    try:
        result = build_template(template, answers, out)
    except AssemblyError as exc:
        rep.add(ERROR, "sample-build-failed",
                "Building a deck from this template failed", str(exc))
        return
    finally:
        pass

    rep.facts["sample_slides"] = result["slides"]
    if result["unresolved_bindings"]:
        rep.add(WARNING, "unresolved-binding",
                "%d value(s) could not be resolved for the test build"
                % len(result["unresolved_bindings"]),
                "\n".join("- %s" % u for u in result["unresolved_bindings"]))
    if result["unfilled_placeholders"]:
        rep.add(WARNING, "unfilled-placeholder",
                "%d placeholder(s) were left visible in the built deck"
                % len(result["unfilled_placeholders"]),
                ", ".join("`{{%s}}`" % p for p in result["unfilled_placeholders"]))
    if validator is not None:
        issues = validator.validate(out)
        if issues:
            rep.add(ERROR, "sample-invalid",
                    "The deck this template builds does not validate",
                    "\n".join("- [%s] %s: %s" % i for i in issues[:15]))
        else:
            rep.add(NOTE, "sample-valid",
                    "A deck built from this template validates cleanly")
    try:
        os.remove(out)
        os.rmdir(tmp)
    except OSError:
        pass


# ---------------------------------------------------------------- markdown
_LABEL = {ERROR: "Error", WARNING: "Warning", NOTE: "Note"}
_GROUP_SHOWN = 15          # individual entries listed before "... and N more"


def _grouped(items):
    """[(code, [findings])] — same code collapses into one entry, worst first."""
    order, groups = [], {}
    for finding in sorted(items, key=lambda x: x.code):
        if finding.code not in groups:
            groups[finding.code] = []
            order.append(finding.code)
        groups[finding.code].append(finding)
    return [(code, groups[code]) for code in order]


def _shared_message(group):
    """A heading for several findings that share a code."""
    messages = {f.message for f in group}
    if len(messages) == 1:
        return group[0].message
    # they differ only by which slide they name; keep the common opening words
    words = [m.split() for m in sorted(messages)]
    common = []
    for parts in zip(*words):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return " ".join(common).rstrip(" :,-") or group[0].code.replace("-", " ")


def _bytes(n):
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def _pseudonyms(rep):
    """Stable stand-in names for block ids, for redacted reports.

    A block id is derived from its slide's title, so on a merged library it can
    itself be client-identifying (`example_corporation`). Dropping ids entirely
    would make the report undiagnosable, so they become `block_01`, `block_02`
    — consistently everywhere, including inside the findings' own wording, so a
    finding can still be discussed by name.
    """
    return {b["id"]: "block_%02d" % n for n, b in enumerate(rep.blocks, 1)}


def _apply_pseudonyms(text, mapping):
    import re as _re
    for real in sorted(mapping, key=len, reverse=True):    # longest first
        text = _re.sub(r'(?<![\w-])%s(?![\w-])' % _re.escape(real),
                       mapping[real], text)
    return text


def to_markdown(rep, include_blocks=True):
    """Render a Report as a document someone can read or send on."""
    f = rep.facts
    errors, warnings, notes = rep.of(ERROR), rep.of(WARNING), rep.of(NOTE)
    out = []

    out.append("# Template check - %s" % f.get("template", "?"))
    out.append("")
    verdict = ("**This template will not build correctly.**" if errors else
               "**No blocking problems.**" if warnings else
               "**Clean.** Nothing to act on.")
    out.append("%s %d error(s), %d warning(s), %d note(s)."
               % (verdict, len(errors), len(warnings), len(notes)))
    out.append("")

    if f.get("redacted"):
        out.append("> Redacted: slide titles and quoted text are replaced with "
                   "`%s`, and block ids - which are derived from those titles - "
                   "with `block_01`, `block_02` and so on, consistently "
                   "throughout. Every count, structural finding and slide "
                   "reference is intact, so this file is safe to share outside "
                   "the firm." % REDACTED)
    else:
        out.append("> **Before sharing:** this report names every block and "
                   "quotes slide titles, which in a proposal template are "
                   "client-identifying. Re-run with `--redact` for a version "
                   "that keeps the findings but drops the text.")
    out.append("")

    # -- summary ------------------------------------------------------
    out.append("## Summary")
    out.append("")
    rows = [("Template", f.get("template")),
            ("Library", f.get("library")),
            ("Size", _bytes(f.get("library_bytes"))),
            ("Slides", f.get("slides")),
            ("Blocks", f.get("blocks")),
            ("Masters / layouts",
             ("%s / %s" % (f["masters"], f["layouts"])) if "masters" in f else None),
            ("Media files", f.get("media")),
            ("Placeholders used / bound",
             ("%s / %s" % (f["placeholders_used"], f["placeholders_bound"]))
             if "placeholders_used" in f else None)]
    if f.get("merged_from_decks"):
        rows.append(("Merged from", "%d decks" % f["merged_from_decks"]))
    if f.get("sample_slides") is not None:
        rows.append(("Test build", "%d slides from %s"
                     % (f["sample_slides"], f.get("sample_answers", "answers"))))
    out.append("| | |")
    out.append("|---|---|")
    for label, value in rows:
        if value is not None:
            out.append("| %s | %s |" % (label, value))
    out.append("")

    if f.get("id_sources"):
        out.append("Block ids came from: "
                   + ", ".join("%s (%d)" % (k, v)
                               for k, v in sorted(f["id_sources"].items())) + ".")
        out.append("")
    if f.get("identified_by"):
        out.append("Slides were matched across decks by: "
                   + ", ".join("%s (%d)" % (k, v)
                               for k, v in sorted(f["identified_by"].items()))
                   + ".")
        out.append("")

    # -- findings -----------------------------------------------------
    for level, items in ((ERROR, errors), (WARNING, warnings), (NOTE, notes)):
        if not items:
            continue
        out.append("## %ss (%d)" % (_LABEL[level], len(items)))
        out.append("")
        for code, group in _grouped(items):
            if len(group) == 1:
                finding = group[0]
                out.append("### %s" % finding.message)
                out.append("")
                meta = "`%s`" % code
                if finding.where:
                    meta += " - %s" % finding.where
                out.append(meta)
                out.append("")
                if finding.detail:
                    out.append(str(finding.detail))
                    out.append("")
                continue

            # One slide with a problem is a finding; a hundred with the same
            # problem is one finding with a list. Reports where the same
            # sentence repeats 136 times are unreadable, and the repetition
            # hides everything else.
            out.append("### %s (%d occurrences)" % (_shared_message(group), len(group)))
            out.append("")
            out.append("`%s`" % code)
            out.append("")
            for finding in group[:_GROUP_SHOWN]:
                line = "- %s" % finding.message
                if finding.where:
                    line += " (%s)" % finding.where
                out.append(line)
            if len(group) > _GROUP_SHOWN:
                out.append("- ... and %d more" % (len(group) - _GROUP_SHOWN))
            out.append("")
            detail = next((f.detail for f in group if f.detail), None)
            if detail:
                out.append(str(detail))
                out.append("")

    # -- blocks -------------------------------------------------------
    if include_blocks and rep.blocks:
        out.append("## Blocks")
        out.append("")
        out.append("| # | Block id | Id from | Slide | Placeholders |")
        out.append("|---|---|---|---|---|")
        for n, b in enumerate(rep.blocks, 1):
            slots = ", ".join("`{{%s}}`" % p for p in b["placeholders"]) or "-"
            out.append("| %d | `%s` | %s | %s | %s |"
                       % (n, b["id"], b["source"], b["slide"], slots))
        out.append("")

    out.append("---")
    out.append("")
    out.append("Generated by `build.py check`. Re-run after any change to the "
               "library, blocks.json or rules.json.")
    out.append("")
    document = "\n".join(out)
    if f.get("redacted"):
        # applied to the finished document, so ids vanish from the findings'
        # own wording too and not merely from the table
        document = _apply_pseudonyms(document, _pseudonyms(rep))
    return document
