"""Reading a library deck: which slide is which block.

A *library* is one `.pptx` someone authored in PowerPoint. The engine never
draws slides — it only selects, orders and fills them — so the only thing it
needs from the deck is a stable **identity per slide**.

Identity is resolved in this order, first hit wins:

1. **Marker** — a text box holding `{{block:cover}}`. Explicit and survives
   anything, including reordering and copy/paste between decks.
2. **Block map** — a `blocks.json` sidecar next to the library, mapping slide
   part -> id. Lets an *unmodified* deck (your firm's existing template) be
   named without opening PowerPoint.
3. **Title** — the slide's title text, slugified. What `import` uses to guess
   the ids it writes into the sidecar.
4. **Position** — `slide7`. Always works, breaks the moment someone reorders
   the deck, so `--inspect` flags it.

The point of 2 and 3: adding a template must never mean editing code, and
should not require editing the deck either.
"""
import os
import re
import zipfile

from . import ooxml, placeholders

# A slide's title: a real title placeholder first, then a shape *named* like one
# (decks built from text boxes rather than layouts — ours included), then simply
# the first text on the slide.
_PH_TITLE = re.compile(
    r'<p:sp>(?:(?!</p:sp>).)*?<p:ph\b[^>]*type="(?:ctrTitle|title)"'
    r'(?:(?!</p:sp>).)*?</p:sp>', re.DOTALL)
_NAMED_TITLE = re.compile(
    r'<p:sp>(?:(?!</p:sp>).)*?<p:cNvPr\b[^>]*name="[^"]*[Tt]itle[^"]*"'
    r'(?:(?!</p:sp>).)*?</p:sp>', re.DOTALL)
_A_T = re.compile(r'<a:t(?:\s[^>]*)?>(.*?)</a:t>', re.DOTALL)


class LibraryError(Exception):
    pass


def slugify(text, fallback="block"):
    """Slide title -> a block id: lower case, words joined by underscores."""
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    s = re.sub(r"_{2,}", "_", s)[:40].strip("_")
    if not s or s[0].isdigit():
        s = (fallback + "_" + s).strip("_")
    return s or fallback


def slide_title(xml):
    """Best-effort title text for a slide. Empty string if it has no text."""
    for pattern in (_PH_TITLE, _NAMED_TITLE):
        m = pattern.search(xml)
        if m:
            text = " ".join(t for t in _A_T.findall(m.group(0)) if t.strip())
            text = ooxml.xml_unescape(text).strip()
            if text and not text.startswith("{{"):
                return re.sub(r"\s+", " ", text)
    for t in _A_T.findall(xml):
        text = ooxml.xml_unescape(t).strip()
        if text and not text.startswith("{{"):
            return re.sub(r"\s+", " ", text)
    return ""


class Block:
    __slots__ = ("id", "part", "index", "source", "title", "placeholders")

    def __init__(self, bid, part, index, source, title, phs):
        self.id, self.part, self.index = bid, part, index
        self.source = source                  # marker | map | title | position
        self.title, self.placeholders = title, phs

    @property
    def marked(self):
        """True when the id is anchored to the slide itself, not its position."""
        return self.source in ("marker", "map")

    def __repr__(self):
        return "<Block %s %s via %s>" % (self.id, self.part, self.source)


class Library:
    """A library deck, opened read-only, indexed by block id.

    `block_map` maps a slide part's file name ('slide7.xml') to a block id —
    the `blocks.json` sidecar. Pass `strict_ids=True` to refuse a deck whose
    slides cannot all be named stably.
    """

    def __init__(self, path, block_map=None, strict_ids=False):
        self.path = str(path)
        if not os.path.exists(self.path):
            raise LibraryError("library not found: %s" % self.path)
        # The sidecar arrives either as {part: id} or as {part: {id, title}};
        # the ids drive indexing, the titles are kept for drift detection.
        self.raw_block_map = dict(block_map or {})
        self.block_map = normalise_block_map(self.raw_block_map)
        self.zf = zipfile.ZipFile(self.path)
        # Anything that fails from here on must close the archive first: on
        # Windows an open handle keeps the file locked, which silently defeats
        # the caller's cleanup (a failed import left its folder behind).
        try:
            self.names = set(self.zf.namelist())
            for required in ("[Content_Types].xml", "ppt/presentation.xml"):
                if required not in self.names:
                    raise LibraryError("%s is not a PowerPoint file (no %s)"
                                       % (os.path.basename(self.path), required))
            self.content_types = self.read("[Content_Types].xml")
            self.presentation = self.read("ppt/presentation.xml")
            self.presentation_rels = self.read("ppt/_rels/presentation.xml.rels")
            self.slide_parts = self._ordered_slide_parts()
            self.blocks = self._index_blocks()
            if strict_ids:
                loose = [b.id for b in self.blocks.values() if not b.marked]
                if loose:
                    raise LibraryError(
                        "%d slide(s) have no stable id (%s). Add a {{block:id}} "
                        "marker or a blocks.json entry."
                        % (len(loose), ", ".join(loose[:5])))
        except Exception:
            self.zf.close()
            raise

    # -- io ---------------------------------------------------------------
    def read(self, part):
        return self.zf.read(part).decode("utf-8", "ignore")

    def read_bytes(self, part):
        return self.zf.read(part)

    def close(self):
        self.zf.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- indexing ---------------------------------------------------------
    def _ordered_slide_parts(self):
        """Slide parts in presentation order — the order the designer sees."""
        rid_to_part = {}
        for tag in ooxml.rel_tags(self.presentation_rels):
            target = ooxml.rel_attr(tag, "Target") or ""
            if re.search(r'/slide"', tag) or ("slides/slide" in target
                                              and "slideLayout" not in tag):
                rid = ooxml.rel_attr(tag, "Id")
                if rid:
                    rid_to_part[rid] = ooxml.resolve_part("ppt/presentation.xml", target)
        parts = []
        for m in re.finditer(r'<p:sldId\b[^>]*?r:id="([^"]+)"[^>]*/>', self.presentation):
            part = rid_to_part.get(m.group(1))
            if part and part in self.names:
                parts.append(part)
        if not parts:
            raise LibraryError("no slides found in %s" % self.path)
        return parts

    def _identify(self, part, xml):
        """-> (block_id, source). See the module docstring for the order."""
        marker = placeholders.block_id(xml)
        if marker:
            return marker, "marker"
        mapped = self.block_map.get(os.path.basename(part))
        if mapped:
            return mapped, "map"
        title = slugify(slide_title(xml), fallback="")
        if title:
            return title, "title"
        return os.path.splitext(os.path.basename(part))[0], "position"

    def _index_blocks(self):
        blocks, seen = {}, {}
        for i, part in enumerate(self.slide_parts):
            xml = self.read(part)
            bid, source = self._identify(part, xml)
            if bid in seen:
                # A duplicate *marker* or *sidecar* id is an authoring error and
                # must be fixed. A duplicate guessed-from-title id is expected —
                # two slides can share a heading — so disambiguate rather than
                # refuse to open the deck.
                if source in ("marker", "map"):
                    raise LibraryError(
                        "duplicate block id %r on %s and %s — block ids must be unique"
                        % (bid, seen[bid], os.path.basename(part)))
                n = 2
                while "%s_%d" % (bid, n) in seen:
                    n += 1
                bid = "%s_%d" % (bid, n)
            seen[bid] = os.path.basename(part)
            blocks[bid] = Block(bid, part, i, source, slide_title(xml),
                                placeholders.find(xml))
        return blocks

    # -- queries ----------------------------------------------------------
    def block(self, bid):
        if bid not in self.blocks:
            raise LibraryError(
                "no block %r in %s (have: %s)"
                % (bid, os.path.basename(self.path), ", ".join(sorted(self.blocks))))
        return self.blocks[bid]

    def ordered(self):
        return sorted(self.blocks.values(), key=lambda b: b.index)

    def all_placeholders(self):
        out = set()
        for b in self.blocks.values():
            out |= b.placeholders
        return out

    def suggest_block_map(self):
        """The sidecar to write at import time: slide part -> id + title.

        Recording the title alongside the id lets `--inspect` notice later that
        a slide was retitled or replaced, which usually means the id is stale.
        """
        return {os.path.basename(b.part): {"id": b.id, "title": b.title}
                for b in self.ordered()}

    def map_drift(self):
        """Sidecar entries that no longer match the deck — retitled slides and
        entries pointing at slides that are gone."""
        drift = []
        present = {os.path.basename(p) for p in self.slide_parts}
        titles = {os.path.basename(b.part): b.title for b in self.blocks.values()}
        for part, entry in self.raw_block_map.items():
            if part not in present:
                drift.append("%s: mapped to %r but that slide is no longer in the deck"
                             % (part, _entry_id(entry)))
            elif isinstance(entry, dict) and entry.get("title") \
                    and entry["title"] != titles.get(part):
                drift.append("%s (%s): title was %r, now %r — check the id still fits"
                             % (part, _entry_id(entry), entry["title"], titles.get(part)))
        return drift

    def summary(self):
        return [{"id": b.id, "slide": os.path.basename(b.part), "source": b.source,
                 "marked": b.marked, "title": b.title,
                 "placeholders": sorted(b.placeholders)}
                for b in self.ordered()]


def _entry_id(entry):
    return entry.get("id") if isinstance(entry, dict) else entry


def normalise_block_map(raw):
    """Accept either {'slide1.xml': 'cover'} or {'slide1.xml': {'id': 'cover'}}."""
    return {k: _entry_id(v) for k, v in (raw or {}).items() if _entry_id(v)}
