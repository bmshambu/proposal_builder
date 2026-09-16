"""Low-level OOXML helpers — the parts of v1 that were about *keeping the file
valid*, lifted out of `harvest.py` and decoupled from harvesting.

Every function here corresponds to something PowerPoint is strict about; see
`docs/v1-learnings.md` §2 before changing any of it. Standard library only.
"""
import os
import re

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
RELS_OPEN = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/'
             '2006/relationships">')

_PRESENTATIONML = "application/vnd.openxmlformats-officedocument.presentationml."
CT_SLIDE = _PRESENTATIONML + "slide+xml"


# ---------------------------------------------------------------- text escaping
def xml_unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- relationships
def rel_tags(rels_xml):
    """Every <Relationship .../> tag in a .rels part, as raw strings."""
    return [m.group(0) for m in re.finditer(r'<Relationship\b[^>]*?/>', rels_xml or "")]


def rel_attr(tag, name):
    m = re.search(r'%s="([^"]*)"' % re.escape(name), tag)
    return m.group(1) if m else None


def rel_ids(rels_xml):
    return set(re.findall(r'Id="([^"]+)"', rels_xml or ""))


def resolve_part(owner_part, target):
    """A relationship Target -> the package part it names.

    Targets may be package-absolute ('/ppt/media/x.png') or relative to the
    directory of the part that owns the .rels ('../media/x.png'). v1 hit both.
    """
    if target.startswith("/"):
        return target[1:]
    base_dir = os.path.dirname(owner_part)
    return os.path.normpath(os.path.join(base_dir, target)).replace("\\", "/")


def rels_part_for(part):
    """'ppt/slides/slide1.xml' -> 'ppt/slides/_rels/slide1.xml.rels'."""
    d = os.path.dirname(part)
    base = os.path.basename(part)
    return ("%s/_rels/%s.rels" % (d, base)) if d else "_rels/%s.rels" % base


def build_rels(tags):
    return XML_DECL + RELS_OPEN + "".join(tags) + "</Relationships>"


class RelIdMinter:
    """Mints relationship ids that collide with nothing already in the part.

    v1's bug: Templafy writes GUID-style ids (`R04cd45cb…`), so "max rIdN + 1"
    computed a wrong max and produced duplicates (REL_ID_INVALID). Skipping
    every id already present — whatever its shape — is the fix.
    """

    def __init__(self, existing=()):
        self.used = set(existing)
        self._n = 0

    def mint(self):
        while True:
            self._n += 1
            cand = "rId%d" % self._n
            if cand not in self.used:
                self.used.add(cand)
                return cand


def keep_referenced_rels(part_xml, rels_xml, always_keep=("slideLayout",)):
    """A slide's .rels reduced to what the slide actually needs.

    Keeps the layout relationship plus every rel the XML references by
    r:id / r:embed / r:link. Dropping a *referenced* rel dangles it (PowerPoint:
    "found a problem with content"); keeping an *unreferenced* one whose part we
    did not copy dangles the target. Both are repair dialogs.
    """
    # ANY attribute in the relationships namespace is a reference, not just the
    # three common ones. SmartArt points at its four parts with r:dm, r:lo,
    # r:qs and r:cs; media uses r:embed and r:link; charts use r:id. Listing
    # them by name means the next one nobody thought of gets silently dropped,
    # and the slide is left referencing a relationship that no longer exists.
    referenced = set(re.findall(r'\br:[A-Za-z]+="([^"]+)"', part_xml))
    kept = []
    for tag in rel_tags(rels_xml):
        rid = rel_attr(tag, "Id")
        if any(k in tag for k in always_keep) or (rid and rid in referenced):
            kept.append(tag)
    return build_rels(kept)


# ---------------------------------------------------------------- content types
def infer_ctype(part):
    """Content type for the standard presentation parts, from the path.

    These parts need their *specific* Override — falling back to the generic
    `.xml` Default makes PowerPoint silently repair the deck.
    """
    p = part.lower()
    if not p.endswith(".xml"):
        return None
    if "/slidemasters/" in p:
        return _PRESENTATIONML + "slideMaster+xml"
    if "/slidelayouts/" in p:
        return _PRESENTATIONML + "slideLayout+xml"
    if "/notesmasters/" in p:
        return _PRESENTATIONML + "notesMaster+xml"
    if "/notesslides/" in p:
        return _PRESENTATIONML + "notesSlide+xml"
    if "/slides/" in p:
        return _PRESENTATIONML + "slide+xml"
    if "/theme/" in p:
        return "application/vnd.openxmlformats-officedocument.theme+xml"
    return None


def ctype_for(ct_xml, part):
    """The content type declared for `part` in a [Content_Types].xml.

    Attribute order is not guaranteed — Templafy writes ContentType before
    PartName — so match the Override by attribute *name*, never by position.
    """
    want = "/" + part.lstrip("/")
    for m in re.finditer(r'<Override\b[^>]*/>', ct_xml):
        tag = m.group(0)
        if rel_attr(tag, "PartName") == want:
            ct = rel_attr(tag, "ContentType")
            if ct:
                return ct
    inferred = infer_ctype(part)
    if inferred:
        return inferred
    ext = os.path.splitext(part)[1].lstrip(".").lower()
    d = re.search(r'<Default\b[^>]*Extension="%s"[^>]*ContentType="([^"]+)"'
                  % re.escape(ext), ct_xml, re.I)
    return d.group(1) if d else None


def drop_override(ct_xml, part):
    return re.sub(r'<Override\b[^>]*PartName="/%s"[^>]*/>' % re.escape(part), "", ct_xml)


def add_overrides(ct_xml, overrides):
    """overrides = [(part, content_type)] — skips any already declared."""
    body = "".join('<Override PartName="/%s" ContentType="%s"/>' % (p, c)
                   for p, c in overrides
                   if c and ('PartName="/%s"' % p) not in ct_xml)
    return ct_xml.replace("</Types>", body + "</Types>")


# ---------------------------------------------------------------- slide surgery
def strip_custdata(xml):
    """Remove <p:custDataLst> (customer-data tag references).

    They point at ppt/tags/* parts. Carrying the reference without the part
    dangles it; several slides sharing one tag part duplicates the reference.
    The tags are non-visual metadata, so dropping them yields a clean deck.
    We author our own slides in v2, but strip defensively — a library deck
    round-tripped through another tool can still pick them up.
    """
    xml = re.sub(r'<p:custDataLst>.*?</p:custDataLst>', '', xml, flags=re.DOTALL)
    return re.sub(r'<p:custDataLst\b[^>]*/>', '', xml)


def remove_shape_containing(xml, needle, tag="p:sp"):
    """Delete the whole `<p:sp>` element that contains `needle`.

    Depth-tracked rather than regex-matched, so a marker inside a grouped shape
    removes only its own shape and never eats the group's closing tag.
    """
    open_re = re.compile(r'<%s(?:\s[^>]*)?>' % re.escape(tag))
    close_tag = "</%s>" % tag
    while True:
        hit = xml.find(needle)
        if hit < 0:
            return xml
        start = None
        for m in open_re.finditer(xml, 0, hit):
            start = m.start()
        if start is None:
            return xml                      # marker outside any shape: leave it
        depth, i = 0, start
        end = None
        while i < len(xml):
            om = open_re.match(xml, i)
            if om:
                depth += 1
                i = om.end()
                continue
            if xml.startswith(close_tag, i):
                depth -= 1
                i += len(close_tag)
                if depth == 0:
                    end = i
                    break
                continue
            i += 1
        if end is None or end <= hit:
            return xml                      # malformed nesting: change nothing
        xml = xml[:start] + xml[end:]
