"""Placeholder discovery and substitution.

v2 uses **explicit** placeholders — `{{ClientName}}` typed into the slide by the
author — instead of v1's inferred tokens. That deletes the whole "is this text
dynamic or static?" class of bug: substitution happens only where an author
asked for it.

Two things here earn their keep:

* `normalise_runs` — PowerPoint splits a placeholder across `<a:r>` runs when
  it is edited character-by-character, so `{{ClientName}}` is never seen whole
  by a naive replacer (v2-plan §8). We stitch those runs back before replacing.
* `apply` — replaces only inside `<a:t>` bodies, using the *fixed* element
  pattern. A loose `<a:t[^>]*>` also matches `<a:tbl>`, `<a:tc>`, `<a:tr>`,
  `<a:tblPr>`, `<a:tab>` — in v1 that swallowed and escaped whole tables into
  garbage. The character after `a:t` must be whitespace or `>`.
"""
import re

from . import ooxml

# The one true text-run pattern. Never loosen it — see the module docstring.
A_T = re.compile(r'(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)', re.DOTALL)
A_P = re.compile(r'(<a:p(?:\s[^>]*)?>)(.*?)(</a:p>)', re.DOTALL)

PLACEHOLDER = re.compile(r'\{\{\s*([A-Za-z0-9_.:]+)\s*\}\}')
BLOCK_MARKER = re.compile(r'\{\{\s*block\s*:\s*([A-Za-z0-9_.-]+)\s*\}\}')


# ---------------------------------------------------------------- run stitching
def _stitch_paragraph(body):
    """Within one <a:p>, pull every split placeholder into a single run.

    The runs' text is concatenated to find placeholders that straddle a run
    boundary; the full placeholder is moved into the first run it touches and
    its characters are removed from the rest. Only `<a:t>` *bodies* change —
    no run is added, removed, or reordered — so formatting and structure hold.
    """
    runs = list(A_T.finditer(body))
    if len(runs) < 2:
        return body

    texts = [ooxml.xml_unescape(m.group(2)) for m in runs]
    spans, pos = [], 0
    for t in texts:
        spans.append((pos, pos + len(t)))
        pos += len(t)
    joined = "".join(texts)

    edits = {}                                  # run index -> new text
    changed = False
    # right-to-left: an edit only shifts text to its right, so offsets computed
    # from the original concatenation stay valid for the placeholders still to
    # be processed. (Two split placeholders can share a run.)
    for pm in reversed(list(PLACEHOLDER.finditer(joined))):
        s, e = pm.span()
        touched = [i for i, (rs, re_) in enumerate(spans) if rs < e and re_ > s]
        if len(touched) < 2:
            continue                            # already whole in one run
        for n, i in enumerate(touched):
            rs, re_ = spans[i]
            cur = edits.get(i, texts[i])
            # slice of this run covered by the placeholder
            lo, hi = max(s, rs) - rs, min(e, re_) - rs
            edits[i] = cur[:lo] + (pm.group(0) if n == 0 else "") + cur[hi:]
        changed = True

    if not changed:
        return body
    out, last = [], 0
    for i, m in enumerate(runs):
        if i in edits:
            out.append(body[last:m.start()])
            out.append(m.group(1) + ooxml.xml_escape(edits[i]) + m.group(3))
            last = m.end()
    out.append(body[last:])
    return "".join(out)


def normalise_runs(xml):
    """Stitch split placeholders across every paragraph in a part."""
    if "{" not in xml:          # a split placeholder may not contain "{{" literally
        return xml
    return A_P.sub(lambda m: m.group(1) + _stitch_paragraph(m.group(2)) + m.group(3), xml)


# ---------------------------------------------------------------- substitution
def find(xml, include_markers=False):
    """Every placeholder name present in a part (after stitching)."""
    names = set(PLACEHOLDER.findall(normalise_runs(xml)))
    if not include_markers:
        names = {n for n in names if not n.lower().startswith("block:")}
    return names


def block_id(xml):
    """The block id declared by this slide's marker, or None."""
    m = BLOCK_MARKER.search(normalise_runs(xml))
    return m.group(1) if m else None


def apply(xml, values):
    """Replace `{{Name}}` with values[Name], inside <a:t> bodies only.

    Unknown placeholders are left untouched and reported by the caller, so an
    author sees the gap rather than a silently blank slide.
    """
    if not values:
        return xml
    xml = normalise_runs(xml)

    def one_run(m):
        raw = ooxml.xml_unescape(m.group(2))
        new = PLACEHOLDER.sub(
            lambda p: str(values[p.group(1)]) if p.group(1) in values else p.group(0),
            raw)
        if new == raw:
            return m.group(0)
        return m.group(1) + ooxml.xml_escape(new) + m.group(3)

    return A_T.sub(one_run, xml)


def strip_marker(xml):
    """Remove the block-marker shape so it never reaches the finished deck."""
    xml = normalise_runs(xml)
    m = BLOCK_MARKER.search(xml)
    if not m:
        return xml
    return ooxml.remove_shape_containing(xml, m.group(0))


# ---------------------------------------------------------------- writing
#
# The other direction: an author points at words on a slide and those words
# become `{{Name}}`. Everything above assumes a placeholder was typed in
# PowerPoint; this is what makes one without leaving the tool.
#
# It is the inverse of `_stitch_paragraph` and it holds the same line: only
# `<a:t>` bodies change. No run is added, removed or reordered, so the run
# properties an author set in PowerPoint - the font, the size, the colour -
# survive untouched, and the placeholder inherits them exactly as a typed one
# would.
#
# Addressing is by paragraph ordinal within the part plus the text that
# paragraph is expected to hold. The text is not belt and braces: it is how a
# stale editor is caught. If the library moved on since the browser read it,
# the ordinal still resolves and would write into the wrong words, and the
# author would never find out. The mismatch refuses instead.

A_BR = re.compile(r'<a:br(?:\s[^>]*)?/>|<a:br(?:\s[^>]*)?>.*?</a:br>', re.DOTALL)
A_FLD = re.compile(r'<a:fld\b.*?</a:fld>', re.DOTALL)

NAME_OK = re.compile(r'^[A-Za-z0-9_.:]+$')


class PlaceholderError(Exception):
    """A placeholder could not be written where it was asked for."""


def paragraph_text(body):
    """One `<a:p>` body as text, with its runs against that text.

    -> (text, [(match, start, end, run text), ...])

    The same rule the editor displays by, because the offsets it sends are
    resolved here: a `<a:br>` contributes a newline and nothing else.
    """
    marks = [("t", m) for m in A_T.finditer(body)]
    marks += [("br", m) for m in A_BR.finditer(body)]
    marks.sort(key=lambda pair: pair[1].start())

    text, runs = "", []
    for kind, m in marks:
        if kind == "br":
            text += "\n"
            continue
        body_text = ooxml.xml_unescape(m.group(2))
        runs.append((m, len(text), len(text) + len(body_text), body_text))
        text += body_text
    return text, runs


def _write_span(body, start, end, name):
    """Put `{{name}}` over characters [start, end) of one paragraph body."""
    text, runs = paragraph_text(body)
    if not 0 <= start < end <= len(text):
        raise PlaceholderError(
            "characters %d to %d are not inside a paragraph of %d character(s)"
            % (start, end, len(text)))

    touched = [r for r in runs if r[1] < end and r[2] > start]
    if not touched:
        raise PlaceholderError("there is no text at that position to replace")

    # A `<a:fld>` is PowerPoint's own - a slide number, a date it maintains.
    # Writing into one produces a placeholder that PowerPoint overwrites the
    # next time the deck is opened, which is a bug nobody would think to look
    # for.
    fields = [(m.start(), m.end()) for m in A_FLD.finditer(body)]
    for m, _s, _e, _t in touched:
        if any(lo <= m.start() < hi for lo, hi in fields):
            raise PlaceholderError(
                "PowerPoint fills that text in itself (a slide number or a "
                "date), so it cannot be made a placeholder")

    token = "{{%s}}" % name
    pieces, cursor, first = [], 0, True
    for m, run_start, run_end, run_text in touched:
        lo = max(start, run_start) - run_start
        hi = min(end, run_end) - run_start
        replaced = run_text[:lo] + (token if first else "") + run_text[hi:]
        first = False
        pieces.append(body[cursor:m.start()])
        pieces.append(m.group(1) + ooxml.xml_escape(replaced) + m.group(3))
        cursor = m.end()
    pieces.append(body[cursor:])
    return "".join(pieces)


def put(xml, edits):
    """Write placeholders into a slide part. -> new xml

    `edits` is a list of dicts: `at` (the paragraph's ordinal in the part),
    `expect` (the text it should hold), `start`, `end`, `name`.

    All or nothing. Every edit is checked before any is applied, so a request
    with one bad address does not leave a half-marked slide behind.
    """
    if not edits:
        return xml
    xml = normalise_runs(xml)

    for edit in edits:
        if not NAME_OK.match(str(edit.get("name") or "")):
            raise PlaceholderError(
                "%r is not a usable name: letters, digits and underscores only"
                % edit.get("name"))

    paragraphs = list(A_P.finditer(xml))
    by_para = {}
    for edit in edits:
        at = int(edit["at"])
        if not 0 <= at < len(paragraphs):
            raise PlaceholderError(
                "this slide has %d paragraph(s), so there is no paragraph %d - "
                "the library has changed since it was read"
                % (len(paragraphs), at))
        by_para.setdefault(at, []).append(edit)

    for at, group in by_para.items():
        body = paragraphs[at].group(2)
        text, _runs = paragraph_text(body)
        expect = group[0].get("expect")
        if expect is not None and text != expect:
            raise PlaceholderError(
                "that text has changed since it was read. It now says %r - "
                "nothing was written" % (text[:70],))
        spans = sorted((int(e["start"]), int(e["end"])) for e in group)
        for (a1, b1), (a2, _b2) in zip(spans, spans[1:]):
            if b1 > a2:
                raise PlaceholderError(
                    "two placeholders would cover the same words")

    # Right to left, so an edit never moves the one before it.
    for at in sorted(by_para, reverse=True):
        body = paragraphs[at].group(2)
        for edit in sorted(by_para[at], key=lambda e: -int(e["start"])):
            body = _write_span(body, int(edit["start"]), int(edit["end"]),
                               edit["name"])
        match = paragraphs[at]
        xml = (xml[:match.start()] + match.group(1) + body + match.group(3)
               + xml[match.end():])
        paragraphs = list(A_P.finditer(xml))
    return xml
