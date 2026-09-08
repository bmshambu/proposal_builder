"""Raw OOXML fragments used to synthesise the demo library deck.

Nothing here is part of the build engine — it exists so we have a *checked-in,
non-confidential* `library.pptx` to develop and regression-test against. Real
templates are authored in PowerPoint by designers (decision D1).
"""

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

NS_P = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')

REL_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
_RT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def rels(entries):
    """entries = [(rId, type_suffix, target)] -> a .rels part."""
    body = "".join('<Relationship Id="%s" Type="%s%s" Target="%s"/>' % (i, _RT, t, g)
                   for i, t, g in entries)
    return XML_DECL + '<Relationships %s>%s</Relationships>' % (REL_NS, body)


# ---------------------------------------------------------------- theme
_SCHEME = [("dk1", "1A1A1A"), ("lt1", "FFFFFF"), ("dk2", "0B2B4A"), ("lt2", "EEF2F6"),
           ("accent1", "00558C"), ("accent2", "1E9BD7"), ("accent3", "6E2585"),
           ("accent4", "00A3A1"), ("accent5", "AC145A"), ("accent6", "FDB913"),
           ("hlink", "0563C1"), ("folHlink", "954F72")]


def theme(name="Demo"):
    clr = "".join('<a:%s><a:srgbClr val="%s"/></a:%s>' % (t, v, t) for t, v in _SCHEME)
    fonts = ('<a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/>'
             '<a:cs typeface=""/></a:majorFont>'
             '<a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/>'
             '<a:cs typeface=""/></a:minorFont>')
    fill = ('<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"><a:tint val="60000"/></a:schemeClr></a:solidFill>'
            '<a:solidFill><a:schemeClr val="phClr"><a:shade val="80000"/></a:schemeClr></a:solidFill>')
    ln = ('<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr">'
          '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
          '<a:prstDash val="solid"/></a:ln>') * 3
    eff = '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
    bgfill = ('<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
              '<a:solidFill><a:schemeClr val="phClr"><a:tint val="95000"/></a:schemeClr></a:solidFill>'
              '<a:solidFill><a:schemeClr val="phClr"><a:shade val="90000"/></a:schemeClr></a:solidFill>')
    return (XML_DECL
            + '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
              ' name="%s"><a:themeElements>' % name
            + '<a:clrScheme name="%s">%s</a:clrScheme>' % (name, clr)
            + '<a:fontScheme name="%s">%s</a:fontScheme>' % (name, fonts)
            + '<a:fmtScheme name="%s">' % name
            + '<a:fillStyleLst>%s</a:fillStyleLst>' % fill
            + '<a:lnStyleLst>%s</a:lnStyleLst>' % ln
            + '<a:effectStyleLst>%s</a:effectStyleLst>' % eff
            + '<a:bgFillStyleLst>%s</a:bgFillStyleLst>' % bgfill
            + '</a:fmtScheme></a:themeElements>'
            + '<a:objectDefaults/><a:extraClrSchemeLst/></a:theme>')


# ---------------------------------------------------------------- shapes
EMU_W, EMU_H = 12192000, 6858000          # 16:9

_SPTREE_HEAD = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
                '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def textbox(sid, name, x, y, cx, cy, lines, size=1800, bold=False,
            color=None, align="l"):
    """A plain text box. `lines` is a list of paragraph strings."""
    fill = ('<a:solidFill><a:srgbClr val="%s"/></a:solidFill>' % color) if color else ""
    rpr = '<a:rPr lang="en-US" sz="%d" b="%d" dirty="0">%s</a:rPr>' % (size, int(bold), fill)
    paras = "".join(
        ('<a:p><a:pPr algn="%s"/><a:r>%s<a:t>%s</a:t></a:r></a:p>' % (align, rpr, esc(t)))
        if t else
        ('<a:p><a:pPr algn="%s"/><a:endParaRPr lang="en-US" sz="%d"/></a:p>' % (align, size))
        for t in lines)
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr txBox="1"/>'
            '<p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            '<p:txBody><a:bodyPr wrap="square" rtlCol="0"><a:normAutofit/></a:bodyPr>'
            '<a:lstStyle/>%s</p:txBody></p:sp>' % (sid, name, x, y, cx, cy, paras))


def band(sid, name, x, y, cx, cy, color="00558C"):
    """A solid rectangle — stands in for branded artwork."""
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            '<a:solidFill><a:srgbClr val="%s"/></a:solidFill><a:ln><a:noFill/></a:ln>'
            '</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/>'
            '</a:p></p:txBody></p:sp>' % (sid, name, x, y, cx, cy, color))


def table(sid, name, x, y, cx, cy, rows, col_widths, header_color="00558C"):
    """A real <a:tbl> graphic frame. Deliberately present in the demo library so
    every build exercises the v1 `<a:t>` table-corruption fix (v1-learnings §2)."""
    grid = "".join('<a:gridCol w="%d"/>' % w for w in col_widths)
    row_h = max(int(cy / max(len(rows), 1)), 370840)
    trs = []
    for ri, row in enumerate(rows):
        head = ri == 0
        tcs = []
        for cell in row:
            fill = (('<a:solidFill><a:srgbClr val="%s"/></a:solidFill>' % header_color)
                    if head else '<a:noFill/>')
            rpr = ('<a:rPr lang="en-US" sz="1200" b="%d" dirty="0">'
                   '<a:solidFill><a:srgbClr val="%s"/></a:solidFill></a:rPr>'
                   % (int(head), "FFFFFF" if head else "1A1A1A"))
            tcs.append('<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>'
                       '<a:p><a:r>%s<a:t>%s</a:t></a:r></a:p></a:txBody>'
                       '<a:tcPr marL="91440" marR="91440" anchor="ctr">%s</a:tcPr></a:tc>'
                       % (rpr, esc(cell), fill))
        trs.append('<a:tr h="%d">%s</a:tr>' % (row_h, "".join(tcs)))
    return ('<p:graphicFrame><p:nvGraphicFramePr>'
            '<p:cNvPr id="%d" name="%s"/><p:cNvGraphicFramePr>'
            '<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/>'
            '</p:nvGraphicFramePr>'
            '<p:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></p:xfrm>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
            '<a:tbl><a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>%s</a:tblGrid>%s</a:tbl>'
            '</a:graphicData></a:graphic></p:graphicFrame>'
            % (sid, name, x, y, cx, cy, grid, "".join(trs)))


def split_run_textbox(sid, name, x, y, cx, cy, pieces, size=1800):
    """A text box whose paragraph is deliberately split across several <a:r> runs.

    Reproduces what PowerPoint does when an author edits a placeholder
    character-by-character ({{Client, Name}} landing in separate runs) — the
    risk called out in v2-plan §8. The build's normalise pass must stitch these
    back together before substitution.
    """
    runs = "".join('<a:r><a:rPr lang="en-US" sz="%d" dirty="0"/><a:t>%s</a:t></a:r>'
                   % (size, esc(p)) for p in pieces)
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr txBox="1"/>'
            '<p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
            '<p:txBody><a:bodyPr wrap="square" rtlCol="0"/><a:lstStyle/>'
            '<a:p>%s</a:p></p:txBody></p:sp>' % (sid, name, x, y, cx, cy, runs))


# ---------------------------------------------------------------- parts
def slide(shapes):
    return (XML_DECL + '<p:sld %s><p:cSld><p:spTree>%s%s</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
            % (NS_P, _SPTREE_HEAD, "".join(shapes)))


def layout(name="Blank"):
    return (XML_DECL
            + '<p:sldLayout %s type="blank" preserve="1"><p:cSld name="%s">'
              '<p:spTree>%s</p:spTree></p:cSld>'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'
            % (NS_P, name, _SPTREE_HEAD))


def master(layout_rid="rId1"):
    clrmap = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
              'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
              'accent6="accent6" hlink="hlink" folHlink="folHlink"/>')
    styles = ('<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="4400"/></a:lvl1pPr>'
              '</p:titleStyle><p:bodyStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr>'
              '</p:bodyStyle><p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr>'
              '</p:otherStyle></p:txStyles>')
    return (XML_DECL
            + '<p:sldMaster %s><p:cSld><p:bg><p:bgPr>'
              '<a:solidFill><a:schemeClr val="bg1"/></a:solidFill><a:effectLst/>'
              '</p:bgPr></p:bg><p:spTree>%s</p:spTree></p:cSld>%s'
              '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="%s"/></p:sldLayoutIdLst>'
              '%s</p:sldMaster>'
            % (NS_P, _SPTREE_HEAD, clrmap, layout_rid, styles))


def presentation(slide_rids, master_rid="rId1"):
    sld_ids = "".join('<p:sldId id="%d" r:id="%s"/>' % (256 + i, r)
                      for i, r in enumerate(slide_rids))
    return (XML_DECL
            + '<p:presentation %s saveSubsetFonts="1">'
              '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="%s"/>'
              '</p:sldMasterIdLst><p:sldIdLst>%s</p:sldIdLst>'
              '<p:sldSz cx="%d" cy="%d"/><p:notesSz cx="%d" cy="%d"/>'
              '</p:presentation>' % (NS_P, master_rid, sld_ids, EMU_W, EMU_H, EMU_H, EMU_W))


def content_types(n_slides):
    base = "application/vnd.openxmlformats-officedocument.presentationml."
    ov = ['<Override PartName="/ppt/presentation.xml" ContentType="%spresentation.main+xml"/>' % base,
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="%sslideMaster+xml"/>' % base,
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="%sslideLayout+xml"/>' % base,
          '<Override PartName="/ppt/theme/theme1.xml" '
          'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>']
    ov += ['<Override PartName="/ppt/slides/slide%d.xml" ContentType="%sslide+xml"/>' % (i, base)
           for i in range(1, n_slides + 1)]
    return (XML_DECL
            + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" '
              'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Default Extension="png" ContentType="image/png"/>'
            + "".join(ov) + '</Types>')
