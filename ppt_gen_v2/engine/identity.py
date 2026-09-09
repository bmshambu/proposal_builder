"""How a slide is recognised as *the same slide* in another deck.

PowerPoint stamps `<a16:creationId id="{GUID}"/>` on shapes (and p14:creationId
on slides). Those survive copying and text edits, so the same slide carries the
same ids in every deck generated from one template — which is what makes it
possible to say two decks contain the same slide when their client names differ.

Read straight out of the raw XML rather than through a parsed tree: it costs
nothing, and it cannot be defeated by anything else in the slide that a full
parse might choke on. Falling back to hashing whole slides is a silent
disaster — every deck's copy differs by its client name, so nothing matches and
a merge de-duplicates nothing at all.
"""
import hashlib
import re

try:
    import pptx_forensics as pf
except ImportError:                                       # pragma: no cover
    pf = None

# PowerPoint stamps <a16:creationId id="{GUID}"/> on shapes (and p14:creationId
# on slides). Read straight out of the raw XML rather than through a parsed
# tree: it costs nothing, and it cannot be defeated by anything else in the
# slide that a full parse might choke on. Falling back to hashing whole slides
# is a silent disaster — every deck's copy of a slide differs by its client
# name, so nothing de-duplicates and you get one block per slide per deck.
_CREATION_ID = re.compile(rb'creationId[^>]*?\bid="\{?([0-9A-Fa-f-]{8,})\}?"')
_A_T_BYTES = re.compile(rb'<a:t(?:\s[^>]*)?>(.*?)</a:t>', re.DOTALL)
_OFF = re.compile(rb'<a:off\b[^>]*?\bx="(-?\d+)"[^>]*?\by="(-?\d+)"')
_EXT = re.compile(rb'<a:ext\b[^>]*?\bcx="(\d+)"[^>]*?\bcy="(\d+)"')


def slide_identity(xml_bytes, notes=None):
    """-> (key, how). The same slide in two decks must give the same key.

    `notes` collects the reason a stronger method was unavailable, so the tool
    can say why it fell back instead of quietly producing a useless library.
    """
    cids = _CREATION_ID.findall(xml_bytes)
    if cids:
        return ("cid", frozenset(c.decode("ascii").lower() for c in cids)), "creationId"

    if pf is not None:
        try:
            data = pf.parse_slide_xml(xml_bytes)
            geom = data.get("geom_sig") or frozenset()
            text = re.sub(r"\s+", " ", (data.get("text") or "")).strip().lower()
            if geom or text:
                return ("struct", geom, text), "structure+text"
        except Exception as exc:
            if notes is not None:
                notes["parse_failed"] = "%s: %s" % (type(exc).__name__, exc)
    elif notes is not None:
        notes.setdefault("no_forensics", "pptx_forensics could not be imported")

    # last resort before hashing everything: geometry alone is token-invariant,
    # so a slide still matches across decks even though its text differs
    geom = frozenset(zip(_OFF.findall(xml_bytes), _EXT.findall(xml_bytes)))
    if geom:
        return ("geom", geom), "geometry"

    body = re.sub(rb"\s+", b" ", xml_bytes)
    return ("sha", hashlib.sha1(body).hexdigest()), "bytes"




def geometry_key(xml_bytes):
    """A token-invariant key: where the shapes are, ignoring what they say.

    Matching a library slide back to the deck it came from cannot rely on text
    — tokenising deliberately rewrites it, so "Example Corporation" on the
    slide becomes "{{ClientName}}" in the library. Shape positions do not move
    when text is substituted, which makes them the right fallback when a deck
    carries no creationIds.
    """
    geom = frozenset(zip(_OFF.findall(xml_bytes), _EXT.findall(xml_bytes)))
    return ("geom", geom) if geom else None
