"""Render a library slide to SVG, in-process.

This is the thumbnail engine for the author console. Rendering happens in
milliseconds with nothing installed, so importing a 60-slide template stays a
normal request instead of a background job, and the result is scalable, tiny,
and *inspectable* — placeholder runs are tagged so the UI can light up exactly
where values land.

**It is an approximation, not PowerPoint.** It draws what a thumbnail needs —
geometry, fills, theme colours, text, tables, images — and represents what it
cannot draw (charts, SmartArt, OLE) as a labelled box rather than guessing.
Fonts and line breaking are estimated, so text will not wrap identically. Use it
to answer "which slide is this, and where do values go?", never "is this
brand-accurate?".

Unlike the assembly path, this module parses with ElementTree rather than regex:
nothing here is written back into a deck, so there are no exact bytes to
preserve, and a real parser is far more reliable for walking a shape tree.

Inheritance is honoured in the order PowerPoint uses — master, then layout, then
the slide — including placeholder geometry, which real firm decks rely on
heavily (their slides often carry no `<a:xfrm>` at all).
"""
import base64
import colorsys
import os
import re
import xml.etree.ElementTree as ET

from . import ooxml, placeholders

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"a": A, "p": P, "r": R}

EMU_PER_PT = 12700
DEFAULT_SIZE = (12192000, 6858000)          # 16:9, if the deck does not say
DEFAULT_INSET = (91440, 45720, 91440, 45720)   # l, t, r, b — PowerPoint's defaults

# Rough advance width per character, as a fraction of font size. Real metrics
# need the actual font; this is deliberately a little generous so wrapped text
# errs towards breaking early rather than overflowing its box.
_ADVANCE = 0.50
_ADVANCE_BOLD = 0.54
_LINE_HEIGHT = 1.2

_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
         "gif": "image/gif", "bmp": "image/bmp", "svg": "image/svg+xml",
         "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff"}
# Vector formats browsers cannot display; drawn as a labelled box instead.
_UNRENDERABLE = {"emf", "wmf", "eps"}

PLACEHOLDER_RE = re.compile(r'\{\{\s*([A-Za-z0-9_.:]+)\s*\}\}')


def _q(tag):
    """'a:xfrm' -> '{ns}xfrm'"""
    prefix, _, local = tag.partition(":")
    return "{%s}%s" % (NS[prefix], local)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------- colour
def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _apply_mods(rgb, el):
    """Theme colours are almost always modified — `lumMod`/`lumOff` in
    particular are how a palette generates its light and dark variants."""
    r, g, b = (int(rgb[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    alpha = 1.0
    for child in el:
        tag = child.tag.split("}")[-1]
        try:
            val = int(child.get("val", "0")) / 100000.0
        except ValueError:
            continue
        if tag == "alpha":
            alpha = val
        elif tag == "shade":
            r, g, b = r * val, g * val, b * val
        elif tag == "tint":
            r, g, b = (c * val + (1 - val) for c in (r, g, b))
        elif tag in ("lumMod", "lumOff", "satMod"):
            h, l, s = colorsys.rgb_to_hls(r, g, b)
            if tag == "lumMod":
                l *= val
            elif tag == "lumOff":
                l += val
            else:
                s *= val
            r, g, b = colorsys.hls_to_rgb(h, _clamp(l), _clamp(s))
    return "#%02X%02X%02X" % tuple(int(_clamp(c) * 255) for c in (r, g, b)), alpha


class Theme:
    """Theme colours plus the master's colour map.

    `schemeClr val="bg1"` does not name a theme colour directly — it goes
    through the master's `<p:clrMap>` (bg1 -> lt1, tx1 -> dk1, ...) and only
    then into the theme's scheme. Missing that indirection is why themed decks
    render with swapped foreground and background.
    """

    FALLBACK = {"dk1": "000000", "lt1": "FFFFFF", "dk2": "44546A", "lt2": "E7E6E6",
                "accent1": "4472C4", "accent2": "ED7D31", "accent3": "A5A5A5",
                "accent4": "FFC000", "accent5": "5B9BD5", "accent6": "70AD47",
                "hlink": "0563C1", "folHlink": "954F72"}

    def __init__(self, theme_xml=None, clr_map=None):
        self.scheme = dict(self.FALLBACK)
        if theme_xml is not None:
            node = theme_xml.find(".//a:themeElements/a:clrScheme", NS)
            for child in (node if node is not None else []):
                name = child.tag.split("}")[-1]
                srgb = child.find("a:srgbClr", NS)
                sys_clr = child.find("a:sysClr", NS)
                if srgb is not None:
                    self.scheme[name] = srgb.get("val", "000000")
                elif sys_clr is not None:
                    self.scheme[name] = sys_clr.get("lastClr", "000000")
        self.map = clr_map or {}

    def resolve(self, name):
        mapped = self.map.get(name, name)
        # phClr is a style placeholder; outside a style context treat it as text
        if mapped == "phClr":
            mapped = "tx1"
        if mapped in ("bg1", "bg2", "tx1", "tx2"):
            mapped = {"bg1": "lt1", "bg2": "lt2", "tx1": "dk1", "tx2": "dk2"}[mapped]
        return self.scheme.get(mapped, self.scheme.get(name, "000000"))


def _colour_from(parent, theme, default=None):
    """-> (css colour, alpha) for the first colour child of `parent`."""
    if parent is None:
        return (default, 1.0) if default else (None, 1.0)
    srgb = parent.find("a:srgbClr", NS)
    if srgb is not None:
        return _apply_mods(srgb.get("val", "000000"), srgb)
    scheme = parent.find("a:schemeClr", NS)
    if scheme is not None:
        return _apply_mods(theme.resolve(scheme.get("val", "tx1")), scheme)
    sys_clr = parent.find("a:sysClr", NS)
    if sys_clr is not None:
        return _apply_mods(sys_clr.get("lastClr", "000000"), sys_clr)
    prst = parent.find("a:prstClr", NS)
    if prst is not None:
        return ("black" if prst.get("val") == "black" else "gray"), 1.0
    return (default, 1.0) if default else (None, 1.0)


def _fill_of(sp_pr, theme):
    """-> (css colour or None, alpha). None means no fill."""
    if sp_pr is None:
        return None, 1.0
    if sp_pr.find("a:noFill", NS) is not None:
        return None, 1.0
    solid = sp_pr.find("a:solidFill", NS)
    if solid is not None:
        return _colour_from(solid, theme)
    grad = sp_pr.find("a:gradFill", NS)
    if grad is not None:                      # approximated by its first stop
        stop = grad.find("a:gsLst/a:gs", NS)
        if stop is not None:
            return _colour_from(stop, theme)
    patt = sp_pr.find("a:pattFill", NS)
    if patt is not None:
        return _colour_from(patt.find("a:fgClr", NS), theme)
    return None, 1.0


def _stroke_of(sp_pr, theme):
    """-> (colour, width EMU, alpha) for a shape outline."""
    if sp_pr is None:
        return None, 0, 1.0
    ln = sp_pr.find("a:ln", NS)
    if ln is None or ln.find("a:noFill", NS) is not None:
        return None, 0, 1.0
    colour, alpha = _colour_from(ln.find("a:solidFill", NS), theme)
    try:
        width = int(ln.get("w", "9525"))
    except ValueError:
        width = 9525
    return colour, width, alpha


# ---------------------------------------------------------------- geometry
def _xfrm_of(el):
    """-> (x, y, cx, cy, rot_degrees, flipH, flipV) in EMU, or None."""
    if el is None:
        return None
    xfrm = el.find("a:xfrm", NS)
    if xfrm is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    try:
        rot = int(xfrm.get("rot", "0")) / 60000.0
        return (int(off.get("x", 0)), int(off.get("y", 0)),
                int(ext.get("cx", 0)), int(ext.get("cy", 0)), rot,
                xfrm.get("flipH") == "1", xfrm.get("flipV") == "1")
    except ValueError:
        return None


def _ph_key(sp):
    """A shape's placeholder identity: (type, idx), or None if it is not one."""
    ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
    if ph is None:
        return None
    return ph.get("type", "body"), ph.get("idx")


def _shape_name(sp):
    for path in ("p:nvSpPr/p:cNvPr", "p:nvPicPr/p:cNvPr",
                 "p:nvGraphicFramePr/p:cNvPr", "p:nvCxnSpPr/p:cNvPr",
                 "p:nvGrpSpPr/p:cNvPr"):
        node = sp.find(path, NS)
        if node is not None:
            return node.get("name", "")
    return ""


# ---------------------------------------------------------------- text
class Run:
    __slots__ = ("text", "size", "bold", "italic", "underline", "colour", "is_ph")

    def __init__(self, text, size, bold, italic, underline, colour, is_ph):
        self.text, self.size = text, size
        self.bold, self.italic, self.underline = bold, italic, underline
        self.colour, self.is_ph = colour, is_ph

    def width(self):
        factor = _ADVANCE_BOLD if self.bold else _ADVANCE
        return len(self.text) * self.size * factor


def _split_placeholders(text):
    """Break text so each {{Placeholder}} is its own run and can be highlighted."""
    out, last = [], 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.start() > last:
            out.append((text[last:m.start()], False))
        out.append((m.group(0), True))
        last = m.end()
    if last < len(text):
        out.append((text[last:], False))
    return out or [(text, False)]


def _runs_of(para, theme, default_size, default_colour):
    """Flatten a paragraph's runs. `<a:br/>` becomes an explicit break marker."""
    out = []
    for child in para:
        tag = child.tag.split("}")[-1]
        if tag == "br":
            out.append(None)
            continue
        if tag not in ("r", "fld"):
            continue
        node = child.find("a:t", NS)
        text = node.text if node is not None and node.text else ""
        if not text:
            continue
        rpr = child.find("a:rPr", NS)
        size, bold, italic, under = default_size, False, False, False
        colour = default_colour
        if rpr is not None:
            if rpr.get("sz"):
                try:
                    size = int(rpr.get("sz")) * EMU_PER_PT // 100
                except ValueError:
                    pass
            bold = rpr.get("b") == "1"
            italic = rpr.get("i") == "1"
            under = rpr.get("u") not in (None, "none")
            found, _alpha = _colour_from(rpr.find("a:solidFill", NS), theme)
            colour = found or colour
        for piece, is_ph in _split_placeholders(text):
            out.append(Run(piece, size, bold, italic, under, colour, is_ph))
    return out


def _wrap(runs, max_width):
    """Greedy word wrap over styled runs -> [[Run, ...], ...] lines.

    SVG has no automatic wrapping, so we do it on estimated glyph widths. A long
    unbroken word is allowed to overflow rather than being chopped mid-word.
    """
    lines, line, used = [], [], 0.0
    for run in runs:
        if run is None:                                  # explicit <a:br/>
            lines.append(line)
            line, used = [], 0.0
            continue
        for word in re.split(r'(\s+)', run.text):
            if not word:
                continue
            piece = Run(word, run.size, run.bold, run.italic, run.underline,
                        run.colour, run.is_ph)
            w = piece.width()
            if used + w > max_width and line and word.strip():
                lines.append(line)
                line, used = [], 0.0
            line.append(piece)
            used += w
    if line:
        lines.append(line)
    return [_merge(_ltrim(ln)) for ln in lines]


def _ltrim(line):
    """Drop the space a wrap leaves at the start of a continuation line."""
    while line and not line[0].text.strip():
        line = line[1:]
    return line


def _merge(line):
    """Coalesce adjacent identically-styled pieces into one run.

    Wrapping works word by word, but emitting a `<tspan>` per word would leave
    whitespace-only spans — and SVG's default `xml:space` collapses those, so
    the words either side would run together. Merging keeps spaces inside a run
    where they survive, and cuts the output size several-fold.
    """
    out = []
    for run in line:
        prev = out[-1] if out else None
        if (prev is not None and prev.size == run.size and prev.bold == run.bold
                and prev.italic == run.italic and prev.underline == run.underline
                and prev.colour == run.colour and prev.is_ph == run.is_ph):
            prev.text += run.text
        else:
            out.append(Run(run.text, run.size, run.bold, run.italic,
                           run.underline, run.colour, run.is_ph))
    return out


def _bullet_prefix(para):
    """The literal bullet a paragraph shows, if any."""
    ppr = para.find("a:pPr", NS)
    if ppr is None:
        return ""
    if ppr.find("a:buNone", NS) is not None:
        return ""
    ch = ppr.find("a:buChar", NS)
    if ch is not None:
        return ch.get("char", "•") + "  "
    if ppr.find("a:buAutoNum", NS) is not None:
        return "1.  "
    return ""


def _render_text(tx_body, box, theme, default_colour="#000000",
                 default_size=228600):
    """Lay a <p:txBody> out inside `box` = (x, y, cx, cy) EMU. -> svg string."""
    if tx_body is None:
        return ""
    x, y, cx, cy = box
    body_pr = tx_body.find("a:bodyPr", NS)
    insets = list(DEFAULT_INSET)
    anchor = "t"
    if body_pr is not None:
        anchor = body_pr.get("anchor", "t")
        for idx, attr in enumerate(("lIns", "tIns", "rIns", "bIns")):
            if body_pr.get(attr) is not None:
                try:
                    insets[idx] = int(body_pr.get(attr))
                except ValueError:
                    pass
    li, ti, ri, bi = insets
    inner_x, inner_w = x + li, max(cx - li - ri, 1)
    inner_y, inner_h = y + ti, max(cy - ti - bi, 1)

    blocks, total_h = [], 0.0
    for para in tx_body.findall("a:p", NS):
        ppr = para.find("a:pPr", NS)
        align = (ppr.get("algn") if ppr is not None else None) or "l"
        indent = 0
        if ppr is not None and ppr.get("marL"):
            try:
                indent = int(ppr.get("marL"))
            except ValueError:
                indent = 0
        runs = _runs_of(para, theme, default_size, default_colour)
        prefix = _bullet_prefix(para)
        if prefix:
            first = next((r for r in runs if r is not None), None)
            if first is not None:
                runs.insert(0, Run(prefix, first.size, first.bold, False, False,
                                   first.colour, False))
        if not runs:                                     # empty paragraph: a gap
            blocks.append(([], align, indent))
            total_h += default_size * _LINE_HEIGHT
            continue
        lines = _wrap(runs, max(inner_w - indent, 1))
        blocks.append((lines, align, indent))
        for line in lines:
            total_h += max((r.size for r in line),
                           default=default_size) * _LINE_HEIGHT

    if anchor == "ctr" and total_h < inner_h:
        cursor = inner_y + (inner_h - total_h) / 2
    elif anchor == "b" and total_h < inner_h:
        cursor = inner_y + (inner_h - total_h)
    else:
        cursor = inner_y

    parts = []
    for lines, align, indent in blocks:
        if not lines:
            cursor += default_size * _LINE_HEIGHT
            continue
        for line in lines:
            size = max((r.size for r in line), default=default_size)
            cursor += size                               # baseline of this line
            width = sum(r.width() for r in line)
            if align in ("ctr", "just"):
                start = inner_x + indent + (inner_w - indent - width) / 2
            elif align == "r":
                start = inner_x + inner_w - width
            else:
                start = inner_x + indent
            spans = []
            for n, run in enumerate(line):
                style = []
                if run.bold:
                    style.append('font-weight="bold"')
                if run.italic:
                    style.append('font-style="italic"')
                if run.underline:
                    style.append('text-decoration="underline"')
                cls = ' class="ph"' if run.is_ph else ""
                pos = (' x="%.0f" y="%.0f"' % (start, cursor)) if n == 0 else ""
                spans.append('<tspan%s%s font-size="%.0f" fill="%s"%s>%s</tspan>'
                             % (pos, cls, run.size, run.colour or default_colour,
                                (" " + " ".join(style)) if style else "",
                                esc(run.text)))
            parts.append('<text xml:space="preserve">%s</text>' % "".join(spans))
            cursor += size * (_LINE_HEIGHT - 1)
    return "".join(parts)


# ---------------------------------------------------------------- shapes
def _geom_svg(sp_pr, box, fill, alpha, stroke, stroke_w):
    """The shape outline itself. Only the common presets are drawn faithfully;
    anything else falls back to a rectangle, which is right often enough for a
    thumbnail and never wrong enough to mislead."""
    x, y, cx, cy = box
    style = []
    style.append('fill="%s"' % (fill if fill else "none"))
    if fill and alpha < 1:
        style.append('fill-opacity="%.3f"' % alpha)
    if stroke:
        style.append('stroke="%s" stroke-width="%d"' % (stroke, max(stroke_w, 1)))
    attrs = " ".join(style)

    prst = "rect"
    if sp_pr is not None:
        node = sp_pr.find("a:prstGeom", NS)
        if node is not None:
            prst = node.get("prst", "rect")

    if prst in ("ellipse", "circle"):
        return ('<ellipse cx="%.0f" cy="%.0f" rx="%.0f" ry="%.0f" %s/>'
                % (x + cx / 2, y + cy / 2, cx / 2, cy / 2, attrs))
    if prst in ("roundRect", "round1Rect", "round2SameRect"):
        r = min(cx, cy) * 0.12
        return ('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" rx="%.0f" %s/>'
                % (x, y, cx, cy, r, attrs))
    if prst in ("line", "straightConnector1"):
        return ('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="%s" '
                'stroke-width="%d"/>'
                % (x, y, x + cx, y + cy, stroke or "#888888", max(stroke_w, 1)))
    if prst == "triangle":
        return ('<polygon points="%.0f,%.0f %.0f,%.0f %.0f,%.0f" %s/>'
                % (x + cx / 2, y, x + cx, y + cy, x, y + cy, attrs))
    return ('<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" %s/>'
            % (x, y, cx, cy, attrs))


def _unsupported_box(box, label):
    """What we draw instead of guessing: a labelled placeholder. Being visibly
    honest about a chart we cannot render beats drawing something wrong."""
    x, y, cx, cy = box
    return ('<g><rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" fill="#F2F4F7" '
            'stroke="#C3CAD3" stroke-width="9525" stroke-dasharray="76200,38100"/>'
            '<text x="%.0f" y="%.0f" font-size="152400" fill="#7A8698" '
            'text-anchor="middle">%s</text></g>'
            % (x, y, cx, cy, x + cx / 2, y + cy / 2, esc(label)))


def _table_svg(tbl, box, theme):
    x, y, cx, cy = box
    cols = [int(c.get("w", 0) or 0) for c in tbl.findall("a:tblGrid/a:gridCol", NS)]
    rows = tbl.findall("a:tr", NS)
    if not cols or not rows:
        return ""
    scale = cx / float(sum(cols)) if sum(cols) else 1.0
    cols = [c * scale for c in cols]
    heights = []
    for tr in rows:
        try:
            heights.append(int(tr.get("h", 0) or 0))
        except ValueError:
            heights.append(0)
    if sum(heights) <= 0:
        heights = [cy / len(rows)] * len(rows)
    else:                                   # honour proportions, fit the frame
        vscale = cy / float(sum(heights))
        heights = [h * vscale for h in heights]

    parts, cursor_y = [], y
    for tr, h in zip(rows, heights):
        cursor_x = x
        for n, tc in enumerate(tr.findall("a:tc", NS)):
            w = cols[n] if n < len(cols) else (cols[-1] if cols else 0)
            cell_box = (cursor_x, cursor_y, w, h)
            fill, alpha = _fill_of(tc.find("a:tcPr", NS), theme)
            parts.append(
                '<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" fill="%s" '
                'fill-opacity="%.3f" stroke="#D0D5DD" stroke-width="6350"/>'
                % (cursor_x, cursor_y, w, h, fill or "#FFFFFF",
                   alpha if fill else 0.0))
            parts.append(_render_text(tc.find("a:txBody", NS), cell_box, theme,
                                      default_size=152400))
            cursor_x += w
        cursor_y += h
    return "".join(parts)


def _image_svg(pic, box, media, rels):
    """Embed the picture as a data URI — the bytes are already in the package."""
    x, y, cx, cy = box
    blip = pic.find("p:blipFill/a:blip", NS)
    rid = blip.get("{%s}embed" % R) if blip is not None else None
    target = rels.get(rid) if rid else None
    if not target:
        return _unsupported_box(box, "image")
    ext = os.path.splitext(target)[1].lstrip(".").lower()
    if ext in _UNRENDERABLE:
        # EMF/WMF are vector formats no browser can display.
        return _unsupported_box(box, "image (%s)" % ext.upper())
    data = media.get(target)
    if not data:
        return _unsupported_box(box, "image")
    uri = "data:%s;base64,%s" % (_MIME.get(ext, "application/octet-stream"),
                                 base64.b64encode(data).decode("ascii"))
    return ('<image x="%.0f" y="%.0f" width="%.0f" height="%.0f" href="%s" '
            'preserveAspectRatio="xMidYMid slice"/>' % (x, y, cx, cy, uri))


class _Painter:
    """Walks a shape tree and emits SVG. One per rendered slide."""

    def __init__(self, theme, media, rels, inherited, default_text="#000000"):
        self.theme, self.media, self.rels = theme, media, rels
        self.inherited = inherited          # placeholder geometry from layout/master
        self.default_text = default_text
        self.unsupported = []

    def box_for(self, el, sp):
        xf = _xfrm_of(el)
        if xf:
            return xf[:4], xf[4]
        key = _ph_key(sp) if sp is not None else None
        if key:
            for candidate in (key, (key[0], None), (None, key[1])):
                if candidate in self.inherited:
                    return self.inherited[candidate], 0
        return None, 0

    def paint(self, tree, skip_placeholders=False, offset=None):
        out = []
        for el in tree:
            tag = el.tag.split("}")[-1]
            if tag == "sp":
                if skip_placeholders and _ph_key(el) is not None:
                    continue            # layout/master prompt boxes never show
                out.append(self._shape(el))
            elif tag == "pic":
                out.append(self._picture(el))
            elif tag == "graphicFrame":
                out.append(self._frame(el))
            elif tag == "grpSp":
                out.append(self._group(el, skip_placeholders))
            elif tag == "cxnSp":
                out.append(self._connector(el))
        return "".join(p for p in out if p)

    def _rotate(self, body, box, rot):
        if not rot or not box:
            return body
        x, y, cx, cy = box
        return ('<g transform="rotate(%.2f %.0f %.0f)">%s</g>'
                % (rot, x + cx / 2, y + cy / 2, body))

    def _shape(self, sp):
        sp_pr = sp.find("p:spPr", NS)
        box, rot = self.box_for(sp_pr, sp)
        if not box:
            return ""
        fill, alpha = _fill_of(sp_pr, self.theme)
        stroke, stroke_w, _a = _stroke_of(sp_pr, self.theme)
        body = _geom_svg(sp_pr, box, fill, alpha, stroke, stroke_w)
        body += _render_text(sp.find("p:txBody", NS), box, self.theme,
                             default_colour=self.default_text)
        return self._rotate(body, box, rot)

    def _picture(self, pic):
        box, rot = self.box_for(pic.find("p:spPr", NS), pic)
        if not box:
            return ""
        return self._rotate(_image_svg(pic, box, self.media, self.rels), box, rot)

    def _frame(self, frame):
        xfrm = frame.find("p:xfrm", NS)
        box = None
        if xfrm is not None:
            off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
            if off is not None and ext is not None:
                try:
                    box = (int(off.get("x", 0)), int(off.get("y", 0)),
                           int(ext.get("cx", 0)), int(ext.get("cy", 0)))
                except ValueError:
                    box = None
        if not box:
            return ""
        tbl = frame.find("a:graphic/a:graphicData/a:tbl", NS)
        if tbl is not None:
            return _table_svg(tbl, box, self.theme)
        data = frame.find("a:graphic/a:graphicData", NS)
        uri = data.get("uri", "") if data is not None else ""
        label = ("chart" if "chart" in uri else
                 "diagram" if "diagram" in uri else
                 "embedded object" if "oleObject" in uri else "graphic")
        self.unsupported.append(label)
        return _unsupported_box(box, label)

    def _group(self, grp, skip_placeholders):
        """A group re-bases its children's coordinates: child offsets are in the
        group's own space (chOff/chExt) and must be mapped onto where the group
        sits on the slide."""
        grp_pr = grp.find("p:grpSpPr", NS)
        xfrm = grp_pr.find("a:xfrm", NS) if grp_pr is not None else None
        inner = self.paint(grp, skip_placeholders)
        if xfrm is None or not inner:
            return inner
        off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
        ch_off, ch_ext = xfrm.find("a:chOff", NS), xfrm.find("a:chExt", NS)
        if None in (off, ext, ch_off, ch_ext):
            return inner
        try:
            x, y = int(off.get("x", 0)), int(off.get("y", 0))
            cx, cy = int(ext.get("cx", 1)), int(ext.get("cy", 1))
            ox, oy = int(ch_off.get("x", 0)), int(ch_off.get("y", 0))
            ocx, ocy = int(ch_ext.get("cx", 1)) or 1, int(ch_ext.get("cy", 1)) or 1
        except ValueError:
            return inner
        sx, sy = cx / float(ocx), cy / float(ocy)
        return ('<g transform="translate(%.0f %.0f) scale(%.4f %.4f) '
                'translate(%.0f %.0f)">%s</g>'
                % (x, y, sx, sy, -ox, -oy, inner))

    def _connector(self, cxn):
        sp_pr = cxn.find("p:spPr", NS)
        box, rot = self.box_for(sp_pr, None)
        if not box:
            return ""
        stroke, stroke_w, _a = _stroke_of(sp_pr, self.theme)
        x, y, cx, cy = box
        return ('<line x1="%.0f" y1="%.0f" x2="%.0f" y2="%.0f" stroke="%s" '
                'stroke-width="%d"/>'
                % (x, y, x + cx, y + cy, stroke or "#888888", max(stroke_w, 1)))


# ---------------------------------------------------------------- the deck
def _rels_map(lib, part):
    """rId -> package part, for one part's relationships."""
    rels_part = ooxml.rels_part_for(part)
    if rels_part not in lib.names:
        return {}
    out = {}
    for tag in ooxml.rel_tags(lib.read(rels_part)):
        rid, target = ooxml.rel_attr(tag, "Id"), ooxml.rel_attr(tag, "Target")
        if rid and target and 'TargetMode="External"' not in tag:
            out[rid] = ooxml.resolve_part(part, target)
    return out


def _first_rel(rels, keyword):
    for target in rels.values():
        if keyword in target:
            return target
    return None


def _parse(lib, part, strip_markers=False):
    """Parse a part, stitching split placeholders first.

    PowerPoint fragments `{{City}}` across `<a:r>` runs when it is edited
    character by character. The build path already stitches those back together
    before substituting; the preview must use the same pass, or it highlights
    `{{Ci`, `ty`, `}}` as three unrelated pieces of text and the author cannot
    see where the value actually lands.
    """
    try:
        xml = placeholders.normalise_runs(lib.read(part))
        if strip_markers:
            # remove the whole marker shape, not just its text, so its (empty)
            # box does not linger in the drawing
            xml = placeholders.strip_marker(xml)
        return ET.fromstring(xml.encode("utf-8"))
    except (ET.ParseError, KeyError, ValueError):
        return None


def _slide_size(lib):
    m = re.search(r'<p:sldSz\b[^>]*cx="(\d+)"[^>]*cy="(\d+)"', lib.presentation)
    return (int(m.group(1)), int(m.group(2))) if m else DEFAULT_SIZE


def _placeholder_boxes(root):
    """Placeholder geometry declared on a layout or master.

    Real templates lean on this: a slide's title shape often carries no
    `<a:xfrm>` at all and inherits its position from the layout. Without this,
    such slides render blank.
    """
    boxes = {}
    tree = root.find("p:cSld/p:spTree", NS) if root is not None else None
    for sp in (tree if tree is not None else []):
        if sp.tag != _q("p:sp"):
            continue
        key = _ph_key(sp)
        xf = _xfrm_of(sp.find("p:spPr", NS))
        if key and xf:
            boxes[key] = xf[:4]
            boxes.setdefault((key[0], None), xf[:4])
            boxes.setdefault((None, key[1]), xf[:4])
    return boxes


def _background(root, theme, size):
    if root is None:
        return ""
    bg = root.find("p:cSld/p:bg", NS)
    if bg is None:
        return ""
    holder = bg.find("p:bgPr", NS)
    if holder is not None:
        colour, alpha = _fill_of(holder, theme)
    else:
        ref = bg.find("p:bgRef", NS)
        if ref is None:
            return ""
        colour, alpha = _colour_from(ref, theme)
    if not colour:
        return ""
    return ('<rect x="0" y="0" width="%d" height="%d" fill="%s" '
            'fill-opacity="%.3f"/>' % (size[0], size[1], colour, alpha))


def render_slide(lib, part, hide_markers=True):
    """Render one library slide part to SVG. -> {"svg", "width", "height", ...}

    Composed the way PowerPoint composes a slide: master, then layout, then the
    slide's own shapes on top. Prompt placeholders on the layout and master are
    skipped — they are authoring hints, not content.
    """
    size = _slide_size(lib)
    slide = _parse(lib, part, strip_markers=hide_markers)
    if slide is None:
        raise ValueError("could not parse %s" % part)

    layout_part = _first_rel(_rels_map(lib, part), "slideLayout")
    layout = _parse(lib, layout_part) if layout_part else None
    master_part = (_first_rel(_rels_map(lib, layout_part), "slideMaster")
                   if layout_part else None)
    master = _parse(lib, master_part) if master_part else None

    theme_xml, clr_map = None, {}
    if master_part:
        theme_part = _first_rel(_rels_map(lib, master_part), "theme")
        if theme_part:
            theme_xml = _parse(lib, theme_part)
        node = master.find("p:clrMap", NS) if master is not None else None
        if node is not None:
            clr_map = dict(node.attrib)
    theme = Theme(theme_xml, clr_map)

    # every image the slide, its layout or its master can reference
    media, rels = {}, {}
    for owner in (master_part, layout_part, part):
        if not owner:
            continue
        for rid, target in _rels_map(lib, owner).items():
            rels[rid] = target
            if target in lib.names and "/media/" in target:
                media.setdefault(target, lib.read_bytes(target))

    inherited = {}
    inherited.update(_placeholder_boxes(master))
    inherited.update(_placeholder_boxes(layout))

    body = [_background(master, theme, size),
            _background(layout, theme, size),
            _background(slide, theme, size)]

    show_master = slide.get("showMasterSp") != "0"
    unsupported = []
    for root, skip in ((master if show_master else None, True),
                       (layout if show_master else None, True),
                       (slide, False)):
        if root is None:
            continue
        tree = root.find("p:cSld/p:spTree", NS)
        if tree is None:
            continue
        painter = _Painter(theme, media, rels, inherited)
        body.append(painter.paint(tree, skip_placeholders=skip))
        unsupported.extend(painter.unsupported)

    inner = "".join(b for b in body if b)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
           'preserveAspectRatio="xMidYMid meet" '
           'font-family="Arial, Helvetica, sans-serif">'
           '<rect x="0" y="0" width="%d" height="%d" fill="#FFFFFF"/>'
           '%s</svg>' % (size[0], size[1], size[0], size[1], inner))
    return {"svg": svg, "width": size[0], "height": size[1],
            "unsupported": sorted(set(unsupported)),
            "placeholders": sorted(set(PLACEHOLDER_RE.findall(inner)))}


def render_block(lib, block_id, **kw):
    return render_slide(lib, lib.block(block_id).part, **kw)


def render_template(template, block_ids=None, **kw):
    """-> [{"id", "title", "svg", ...}] for a template's blocks, in deck order."""
    out = []
    with template.open_library() as lib:
        wanted = list(block_ids) if block_ids else [b.id for b in lib.ordered()]
        for bid in wanted:
            block = lib.block(bid)
            try:
                result = render_block(lib, bid, **kw)
            except Exception as exc:           # one bad slide must not break the set
                result = {"svg": None, "width": 0, "height": 0,
                          "placeholders": [], "unsupported": [],
                          "error": "%s: %s" % (type(exc).__name__, exc)}
            result.update({"id": bid, "title": block.title, "source": block.source,
                           "slide": os.path.basename(block.part)})
            out.append(result)
    return out


# ---------------------------------------------------------------- text card
def block_card(lib, block_id):
    """A text-only description of a block — the zero-risk fallback.

    Useful on its own (search, accessibility, a diff view) and as the answer for
    slides the renderer cannot do justice: it never lies about what is there.
    """
    block = lib.block(block_id)
    xml = lib.read(block.part)
    root = _parse(lib, block.part)
    texts = []
    if root is not None:
        tree = root.find("p:cSld/p:spTree", NS)
        for node in (tree.iter(_q("a:t")) if tree is not None else []):
            if node.text and node.text.strip() and "{{block:" not in node.text:
                texts.append(node.text.strip())
    return {"id": block_id, "title": block.title, "source": block.source,
            "slide": os.path.basename(block.part), "text": texts,
            "placeholders": sorted(block.placeholders),
            "tables": xml.count("<a:tbl>"), "images": xml.count("<p:pic>")}
