"""Putting placeholders back into a library whose values are already filled in.

A deck Templafy generated has its values substituted: the cover says
"Example Corporation", not "{{ClientName}}". A library built from such decks is
therefore a **snapshot of one client**, not a template — it will build the same
deck for everyone. Restoring the placeholders is what makes it reusable.

This is v1's token detection run backwards, and it inherits v1's warning
(`docs/v1-learnings.md` §3): a value can be both dynamic and static on the same
slide — "New York" is a city answer *and* an office address in the boilerplate.
So it is opt-in, matches whole words only, and reports every replacement it
made so a human can check them.

`tokenise_library()` works on a template that already exists, which matters:
once you have a library PowerPoint is happy with, you do not want to rebuild it
from scratch just to add placeholders.
"""
import os
import re
import shutil
import zipfile

from . import ooxml
from .bindings import date_formats
from .library import Library

# Most payload answers *select slides*. Only a few are literal text in the deck:
# the client names, the date, and the city — the same short list v1 arrived at.
DEFAULT_TOKENS = {
    "FullClientName": "ClientName",
    "ShortClientName": "ShortClientName",
    "DueDate": "DueDate",
}
MIN_TOKEN_LEN = 4          # never swap something as short as a code or initial

_A_T = re.compile(r'(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)', re.DOTALL)


def guess_token_map(payload):
    """Payload field -> placeholder name, for the text-substitution fields."""
    token_map = {}
    for field in payload:
        if field in DEFAULT_TOKENS:
            token_map[field] = DEFAULT_TOKENS[field]
        elif "city" in field.lower():
            token_map[field] = "City"
    return token_map


def replacements(payload, token_map):
    """[(literal, placeholder, field, format)] longest-first.

    A date is written many ways ("November 30, 2026", "30/11/2026"). We swap
    whichever the deck actually used and remember *which*, or the binding has
    no way to render it back and a rebuild shows a raw 20261130.
    """
    reps = []
    for field, name in token_map.items():
        value = payload.get(field)
        if value in (None, "", True, False):
            continue
        text = str(value)
        variants = date_formats(text)
        if variants:
            for fmt, rendered in variants.items():
                if len(rendered) >= MIN_TOKEN_LEN:
                    reps.append((rendered, "{{%s}}" % name, field, fmt))
        elif len(text) >= MIN_TOKEN_LEN:
            reps.append((text, "{{%s}}" % name, field, None))
    reps.sort(key=lambda r: -len(r[0]))   # "Example Corporation" before "Example"
    return reps


def tokenise_part(xml, reps):
    """-> (new xml, {literal: count}, {field: {format: count}}).

    Substitution happens only inside <a:t>, and only on whole words, so
    "Example" never turns "Examples" into "{{ShortClientName}}s" (v1 §2).
    """
    if not reps:
        return xml, {}, {}
    hits, formats = {}, {}
    compiled = [(re.compile(r'(?<!\w)' + re.escape(lit) + r'(?!\w)'), lit, ph, fld, fmt)
                for lit, ph, fld, fmt in reps]

    def one(m):
        raw = ooxml.xml_unescape(m.group(2))
        new = raw
        for pattern, lit, ph, fld, fmt in compiled:
            new, n = pattern.subn(ph, new)
            if n:
                hits[lit] = hits.get(lit, 0) + n
                if fmt:
                    formats.setdefault(fld, {})
                    formats[fld][fmt] = formats[fld].get(fmt, 0) + n
        if new == raw:
            return m.group(0)
        return m.group(1) + ooxml.xml_escape(new) + m.group(3)

    return _A_T.sub(one, xml), hits, formats


def chosen_formats(fmt_counts):
    """The date rendering a deck used most often, per field."""
    return {field: max(counts.items(), key=lambda kv: kv[1])[0]
            for field, counts in fmt_counts.items() if counts}


def bindings_for(token_map, formats):
    """The `placeholders` block for rules.json."""
    out = {}
    for field, name in token_map.items():
        binding = {"from": "field", "field": field}
        if field in formats:
            binding["format"] = formats[field]
        out["{{%s}}" % name] = binding
    return out


def tokenise_library(template, payload, token_map=None, backup=True):
    """Rewrite a template's library, restoring placeholders. -> report dict.

    Only slide parts are touched, and only the text inside `<a:t>`. Every other
    part is copied through byte for byte, so nothing structural can change —
    which matters when the library you are editing is one PowerPoint has only
    just agreed to open.
    """
    token_map = dict(token_map or {}) or guess_token_map(payload)
    reps = replacements(payload, token_map)
    if not reps:
        return {"replacements": {}, "map": token_map, "formats": {},
                "slides_changed": 0, "backup": None}

    path = template.library_path
    with Library(path) as lib:
        slide_parts = set(lib.slide_parts)

    totals, fmt_counts, changed = {}, {}, 0
    source = zipfile.ZipFile(path)
    staged = path + ".tokenising"
    try:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as out:
            for info in source.infolist():
                data = source.read(info.filename)
                if info.filename in slide_parts:
                    xml = data.decode("utf-8", "ignore")
                    new_xml, hits, formats = tokenise_part(xml, reps)
                    if hits:
                        data = new_xml.encode("utf-8")
                        changed += 1
                    for literal, n in hits.items():
                        totals[literal] = totals.get(literal, 0) + n
                    for field, seen in formats.items():
                        for fmt, n in seen.items():
                            fmt_counts.setdefault(field, {})
                            fmt_counts[field][fmt] = fmt_counts[field].get(fmt, 0) + n
                out.writestr(info, data)
    finally:
        source.close()

    saved = None
    if backup:
        saved = path + ".before-tokenise"
        if not os.path.exists(saved):
            shutil.copy(path, saved)
    os.replace(staged, path)

    retitled = _refresh_recorded_titles(template)

    return {"replacements": totals, "map": token_map,
            "formats": chosen_formats(fmt_counts),
            "slides_changed": changed, "retitled": retitled, "backup": saved}


def _refresh_recorded_titles(template):
    """Update the titles `blocks.json` records, now the text has changed.

    Substitution rewrites the very text a title is read from — a cover reading
    "Example Corporation" becomes "{{ClientName}}". The sidecar stores the
    title each id was assigned against, so leaving it alone makes every
    tokenised slide report drift immediately afterwards, and buries a real
    warning under a self-inflicted one.

    Only the recorded titles move; the ids and their slides do not.
    """
    raw = template.raw_block_map
    if not raw:
        return 0
    with Library(template.library_path) as lib:
        titles = {os.path.basename(b.part): b.title for b in lib.ordered()}
    updated, changed = {}, 0
    for part, entry in raw.items():
        if isinstance(entry, dict) and part in titles \
                and entry.get("title") != titles[part]:
            updated[part] = dict(entry, title=titles[part])
            changed += 1
        else:
            updated[part] = entry
    if changed:
        template.write_block_map(updated)
    return changed
