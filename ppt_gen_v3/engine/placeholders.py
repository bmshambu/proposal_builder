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
