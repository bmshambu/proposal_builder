"""The rules engine: (answers + rules.json) -> an ordered list of block ids.

This is the piece that replaces v1's harvesting. In v1 the deltas were *inferred*
by diffing generated decks; here they are *declared* by us, so the whole
selection is auditable by reading one file.

Rules shape (v2-plan §3):

    {
      "baseline": ["cover", "about_us", "approach", "scope", "fees"],
      "blocks": {
        "cover":            {"slides": ["cover"], "when": "always"},
        "expansion_detail": {"slides": ["expansion_detail"],
                             "when": {"field": "AuditType",
                                      "eq": "Expansion of Services"},
                             "insert_after": "approach"},
        "scope":            {"slides": ["scope"], "when": "always",
                             "variant": {"when": {...}, "slides": ["scope_expansion"]}}
      },
      "placeholders": {"{{ClientName}}": {"from": "field", "field": "FullClientName"}}
    }

`baseline` is the spine: the deck at default answers, in order. Conditional
blocks name where they slot in with `insert_after`. A `variant` swaps a block's
*content* without moving it.
"""
import json
import os
import re


class RulesError(Exception):
    pass


# ---------------------------------------------------------------- answers
def flatten(obj, prefix=""):
    """Nested answers -> flat dot-path dict, so rules can name `Client.City`."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, "%s.%s" % (prefix, k) if prefix else str(k)))
    elif isinstance(obj, list):
        out[prefix] = obj
        for i, v in enumerate(obj):
            out.update(flatten(v, "%s[%d]" % (prefix, i)))
    else:
        out[prefix] = obj
    return out


# ---------------------------------------------------------------- conditions
def evaluate(cond, answers):
    """True if `cond` holds for the flattened `answers`.

    Supported: "always" / null, {field, eq|ne|in|not_in|exists},
    {"all": [...]}, {"any": [...]}, {"not": cond}. Deliberately small — add an
    operator only when a real template needs it.
    """
    if cond is None or cond == "always" or cond == {}:
        return True
    if cond == "never":
        return False
    if not isinstance(cond, dict):
        raise RulesError("condition must be an object or \"always\": %r" % (cond,))

    if "all" in cond:
        return all(evaluate(c, answers) for c in cond["all"])
    if "any" in cond:
        return any(evaluate(c, answers) for c in cond["any"])
    if "not" in cond:
        return not evaluate(cond["not"], answers)

    field = cond.get("field")
    if not field:
        raise RulesError("condition needs a \"field\": %r" % (cond,))
    actual = answers.get(field)

    if "exists" in cond:
        present = actual is not None and actual != ""
        return present == bool(cond["exists"])
    if "eq" in cond:
        return _same(actual, cond["eq"])
    if "ne" in cond:
        return not _same(actual, cond["ne"])
    if "in" in cond:
        return any(_same(actual, v) for v in cond["in"])
    if "not_in" in cond:
        return not any(_same(actual, v) for v in cond["not_in"])
    raise RulesError("condition has no operator: %r" % (cond,))


def _same(a, b):
    """Compare as strings, case- and whitespace-insensitively.

    Answers arrive from forms and JSON with inconsistent casing/spacing; a rule
    silently failing to match because of a trailing space is a bad failure mode.
    """
    if a is None or b is None:
        return a is b
    return re.sub(r"\s+", " ", str(a)).strip().lower() == \
           re.sub(r"\s+", " ", str(b)).strip().lower()


# ---------------------------------------------------------------- the rules
class Rules:
    def __init__(self, spec, source="<dict>"):
        self.source = source
        self.baseline = list(spec.get("baseline") or [])
        self.blocks = dict(spec.get("blocks") or {})
        self.placeholders = dict(spec.get("placeholders") or {})
        if not self.baseline and not self.blocks:
            raise RulesError("%s declares neither a baseline nor any blocks" % source)

    @classmethod
    def load(cls, path):
        path = str(path)
        if not os.path.exists(path):
            raise RulesError("rules file not found: %s" % path)
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), source=os.path.basename(path))

    # -- selection --------------------------------------------------------
    def _slides_for(self, bid, answers):
        """The library slides a block contributes, honouring its variant."""
        spec = self.blocks.get(bid)
        if spec is None:
            return [bid]                       # bare id in baseline == its own slide
        for variant in _as_list(spec.get("variant")):
            if evaluate(variant.get("when"), answers):
                return list(variant.get("slides") or [bid])
        return list(spec.get("slides") or [bid])

    def select(self, answers):
        """-> (ordered [(block_id, slide_id)], trace) for these answers."""
        flat = flatten(answers)
        trace = []

        # 1. the spine, filtered by each block's own condition
        order = []
        for bid in self.baseline:
            spec = self.blocks.get(bid, {})
            if evaluate(spec.get("when", "always"), flat):
                order.append(bid)
            else:
                trace.append("skip %s (baseline block, condition false)" % bid)

        # 2. conditional blocks slotted in via insert_after
        #
        # A block may anchor to another conditional block ("this always follows
        # that"), so placement runs in passes: each pass places whatever has its
        # anchor already down. Doing it in one sorted sweep would append a block
        # whose anchor simply had not been placed yet, which made the output
        # order depend on how the ids happened to sort.
        pending = [(bid, spec) for bid, spec in sorted(self.blocks.items())
                   if bid not in self.baseline
                   and evaluate((spec or {}).get("when", "always"), flat)]
        while pending:
            progressed = []
            for bid, spec in pending:
                anchor = (spec or {}).get("insert_after")
                if anchor is None:
                    order.append(bid)
                    trace.append("add %s (appended: no insert_after)" % bid)
                elif anchor in order:
                    order.insert(order.index(anchor) + 1, bid)
                    trace.append("add %s (after %s)" % (bid, anchor))
                else:
                    continue                     # anchor not placed yet: retry
                progressed.append((bid, spec))
            if not progressed:
                # every remaining anchor is absent from this deck (its own
                # condition was false, or it is a typo) — append, and say so
                for bid, spec in pending:
                    order.append(bid)
                    trace.append("add %s (anchor %r absent — appended)"
                                 % (bid, (spec or {}).get("insert_after")))
                break
            pending = [p for p in pending if p not in progressed]

        # 3. resolve each block to its slides (variants applied here)
        resolved = []
        for bid in order:
            slides = self._slides_for(bid, flat)
            declared = list((self.blocks.get(bid) or {}).get("slides") or [bid])
            if slides != declared:
                trace.append("variant %s -> %s" % (bid, ", ".join(slides)))
            for sid in slides:
                resolved.append((bid, sid))
        return resolved, trace

    # -- validation -------------------------------------------------------
    def check_against(self, lib):
        """Problems that would only surface at build time, found up front."""
        problems = []
        referenced = set()
        for bid in set(self.baseline) | set(self.blocks):
            spec = self.blocks.get(bid) or {}
            referenced.update(spec.get("slides") or [bid])
            for variant in _as_list(spec.get("variant")):
                referenced.update(variant.get("slides") or [])
        for sid in sorted(referenced):
            if sid not in lib.blocks:
                problems.append("rules reference block %r, which the library lacks" % sid)
        for bid in self.baseline:
            if bid not in self.blocks and bid not in lib.blocks:
                problems.append("baseline lists %r, which is neither a rule nor a block" % bid)
        for bid, spec in self.blocks.items():
            anchor = (spec or {}).get("insert_after")
            if anchor and anchor not in self.baseline and anchor not in self.blocks:
                problems.append("block %r anchors after %r, which is not in the deck" % (bid, anchor))
        return problems


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]
