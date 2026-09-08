#!/usr/bin/env python3
"""Build a contact sheet: every block in a template, rendered to SVG.

This is how you judge the renderer. Slide thumbnails are the one part of this
project you cannot verify from a test assertion — a slide can be structurally
perfect and still look wrong — so the output is a page a human looks at.

    python tools/make_contact_sheet.py demo -o out/contact_sheet.html

The page body is written without <html>/<head>/<body> wrappers so it can be
published as an Artifact directly, or opened as a local file.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from engine import Template, find_template            # noqa: E402
from engine import svg as svgmod                      # noqa: E402

CSS = """
:root {
  --ground:   #F4F6F9;
  --surface:  #FFFFFF;
  --sunken:   #E9EDF2;
  --ink:      #101820;
  --ink-soft: #55616F;
  --ink-faint:#8894A3;
  --line:     #DCE3EB;
  --line-firm:#C2CCD8;
  --brand:    #00558C;
  --brand-2:  #00A3A1;
  --slot:     #B45309;
  --slot-bg:  #FDF3E3;
  --ok:       #1F7A55;
}
:root:not([data-theme="light"]) {
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:   #0D1117;
    --surface:  #151C24;
    --sunken:   #1D2630;
    --ink:      #E7EDF4;
    --ink-soft: #9EACBC;
    --ink-faint:#6C7A8B;
    --line:     #253039;
    --line-firm:#33414E;
    --brand:    #4FA3D9;
    --brand-2:  #35C4C1;
    --slot:     #F0A860;
    --slot-bg:  #33271A;
    --ok:       #4CBF8B;
  }
}
:root[data-theme="dark"] {
  --ground:   #0D1117;
  --surface:  #151C24;
  --sunken:   #1D2630;
  --ink:      #E7EDF4;
  --ink-soft: #9EACBC;
  --ink-faint:#6C7A8B;
  --line:     #253039;
  --line-firm:#33414E;
  --brand:    #4FA3D9;
  --brand-2:  #35C4C1;
  --slot:     #F0A860;
  --slot-bg:  #33271A;
  --ok:       #4CBF8B;
}

* { box-sizing: border-box; }
body {
  background: var(--ground);
  color: var(--ink);
  font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Segoe UI",
               system-ui, sans-serif;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1240px; margin: 0 auto; padding: 40px 28px 72px; }

/* ---- masthead ---- */
.masthead { display: flex; flex-wrap: wrap; gap: 28px 40px;
            align-items: flex-end; justify-content: space-between;
            padding-bottom: 22px; border-bottom: 2px solid var(--ink); }
.eyebrow { font-family: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo,
                        Consolas, monospace;
           font-size: 11px; letter-spacing: .16em; text-transform: uppercase;
           color: var(--ink-faint); margin: 0 0 8px; }
h1 { font-size: clamp(28px, 4vw, 40px); line-height: 1.1; margin: 0;
     letter-spacing: -.02em; text-wrap: balance; font-weight: 600; }
.standfirst { margin: 12px 0 0; max-width: 60ch; color: var(--ink-soft);
              font-size: 15px; }

.facts { display: flex; gap: 30px; flex-wrap: wrap; }
.fact { min-width: 78px; }
.fact dt { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 10px;
           letter-spacing: .14em; text-transform: uppercase;
           color: var(--ink-faint); margin: 0 0 3px; }
.fact dd { margin: 0; font-size: 22px; font-weight: 600;
           font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.fact dd span { font-size: 13px; font-weight: 400; color: var(--ink-soft); }

/* ---- controls ---- */
.controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
            margin: 26px 0 8px; }
.toggle { display: inline-flex; align-items: center; gap: 9px;
          background: var(--surface); border: 1px solid var(--line-firm);
          border-radius: 999px; padding: 7px 15px 7px 12px; cursor: pointer;
          font-size: 13.5px; color: var(--ink); font-family: inherit; }
.toggle:hover { border-color: var(--brand); }
.toggle:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
.toggle .dot { width: 11px; height: 11px; border-radius: 50%;
               background: var(--line-firm); flex: none; }
.toggle[aria-pressed="true"] { border-color: var(--slot); color: var(--slot); }
.toggle[aria-pressed="true"] .dot { background: var(--slot); }
.hint { font-size: 13px; color: var(--ink-faint); }

/* ---- the sheet ---- */
.sheet { display: grid; gap: 26px; margin: 22px 0 0;
         grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
.cell { background: var(--surface); border: 1px solid var(--line);
        border-radius: 3px; overflow: hidden; display: flex;
        flex-direction: column; }
.frame { position: relative; display: block; width: 100%; padding: 0;
         border: 0; border-bottom: 1px solid var(--line);
         background: var(--sunken); cursor: zoom-in; line-height: 0; }
.frame:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
.frame svg { width: 100%; height: auto; display: block; }
.seq { position: absolute; top: 8px; left: 8px; z-index: 2;
       font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 10px;
       background: rgba(16,24,32,.72); color: #fff; border-radius: 2px;
       padding: 2px 6px; letter-spacing: .06em; line-height: 1.5; }

.caption { padding: 12px 14px 14px; display: flex; flex-direction: column;
           gap: 8px; flex: 1; }
.bid { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 13px;
       font-weight: 500; color: var(--ink); overflow-wrap: anywhere; }
.meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
        font-size: 11px; color: var(--ink-faint); }
.tag { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 10px;
       letter-spacing: .06em; text-transform: uppercase; padding: 2px 6px;
       border: 1px solid var(--line-firm); border-radius: 2px;
       color: var(--ink-soft); }
.slots { display: flex; flex-wrap: wrap; gap: 5px; margin-top: auto; }
.slot { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 11px;
        background: var(--slot-bg); color: var(--slot); border-radius: 2px;
        padding: 2px 6px; overflow-wrap: anywhere; }
.none { font-size: 11.5px; color: var(--ink-faint); font-style: italic; }

/* Placeholder runs are tagged in the SVG itself, so the highlight is the real
   thing the author console will show - not a mock-up of it. */
.sheet.lit .ph { fill: var(--slot) !important; font-weight: 700; }
dialog.lit .ph  { fill: var(--slot) !important; font-weight: 700; }

/* ---- caveats ---- */
.notes { margin: 46px 0 0; padding: 24px 26px; background: var(--surface);
         border: 1px solid var(--line); border-left: 3px solid var(--brand);
         border-radius: 2px; }
.notes h2 { margin: 0 0 4px; font-size: 17px; font-weight: 600;
            letter-spacing: -.01em; }
.notes p { margin: 0 0 16px; color: var(--ink-soft); font-size: 14px;
           max-width: 66ch; }
.grid2 { display: grid; gap: 20px 34px;
         grid-template-columns: repeat(auto-fit, minmax(258px, 1fr)); }
.grid2 h3 { margin: 0 0 7px; font-family: "IBM Plex Mono", ui-monospace, monospace;
            font-size: 10.5px; letter-spacing: .13em; text-transform: uppercase;
            color: var(--ink-faint); font-weight: 500; }
.grid2 ul { margin: 0; padding-left: 17px; font-size: 13.5px;
            color: var(--ink-soft); }
.grid2 li { margin-bottom: 5px; }
.grid2 li b { color: var(--ink); font-weight: 600; }

footer { margin-top: 34px; font-size: 12.5px; color: var(--ink-faint);
         font-family: "IBM Plex Mono", ui-monospace, monospace; }

/* ---- zoom ---- */
dialog { border: 1px solid var(--line-firm); border-radius: 4px; padding: 0;
         background: var(--surface); color: var(--ink); max-width: min(1180px, 94vw);
         width: 100%; }
dialog::backdrop { background: rgba(8, 12, 17, .74); }
dialog svg { width: 100%; height: auto; display: block; background: var(--sunken); }
.dlg-bar { display: flex; justify-content: space-between; align-items: center;
           gap: 16px; padding: 11px 14px; border-top: 1px solid var(--line);
           font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12.5px; }
.dlg-bar button { font: inherit; background: var(--ink); color: var(--ground);
                  border: 0; border-radius: 2px; padding: 6px 13px; cursor: pointer; }
.dlg-bar button:focus-visible { outline: 2px solid var(--brand);
                                outline-offset: 2px; }

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
"""

JS = """
(function () {
  var sheet = document.getElementById('sheet');
  var lit = document.getElementById('lit');
  var dlg = document.getElementById('zoom');
  var stage = document.getElementById('stage');
  var label = document.getElementById('zoom-label');

  lit.addEventListener('click', function () {
    var on = lit.getAttribute('aria-pressed') === 'true';
    lit.setAttribute('aria-pressed', String(!on));
    sheet.classList.toggle('lit', !on);
    dlg.classList.toggle('lit', !on);
    lit.querySelector('.txt').textContent =
      !on ? 'Placeholders highlighted' : 'Highlight placeholders';
  });

  Array.prototype.forEach.call(
    document.querySelectorAll('.frame'), function (btn) {
      btn.addEventListener('click', function () {
        stage.innerHTML = btn.querySelector('svg').outerHTML;
        label.textContent = btn.getAttribute('data-id');
        dlg.showModal();
      });
    });

  document.getElementById('close').addEventListener('click', function () {
    dlg.close();
  });
  dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });
})();
"""


def build_page(template, results):
    total_svg = sum(len(r["svg"] or "") for r in results)
    slots = sorted({p for r in results for p in r["placeholders"]})
    unsupported = sorted({u for r in results for u in (r.get("unsupported") or [])})
    failed = [r for r in results if r.get("error")]

    cells = []
    for n, r in enumerate(results, 1):
        if r.get("error"):
            body = ('<div style="padding:38px 16px;text-align:center;'
                    'color:var(--ink-faint);font-size:13px">could not render: %s</div>'
                    % _esc(r["error"]))
            frame = '<div class="frame"><span class="seq">%02d</span>%s</div>' % (n, body)
        else:
            frame = ('<button class="frame" data-id="%s" '
                     'aria-label="Enlarge %s"><span class="seq">%02d</span>%s</button>'
                     % (_esc(r["id"]), _esc(r["id"]), n, r["svg"]))
        chips = "".join('<span class="slot">{{%s}}</span>' % _esc(p)
                        for p in r["placeholders"]) \
            or '<span class="none">no placeholders</span>'
        cells.append(
            '<figure class="cell">%s<figcaption class="caption">'
            '<span class="bid">%s</span>'
            '<span class="meta"><span class="tag">%s</span>%s</span>'
            '<span class="slots">%s</span>'
            '</figcaption></figure>'
            % (frame, _esc(r["id"]), _esc(r["source"]), _esc(r["slide"]), chips))

    caveat_items = "".join(
        "<li>%s</li>" % s for s in [
            "<b>Text wrapping is estimated.</b> Real metrics need the actual "
            "font, so line breaks will not match PowerPoint exactly.",
            "<b>Fonts fall back</b> to the browser's sans-serif unless the "
            "family is installed.",
            "<b>Gradients</b> render as their first stop.",
            "<b>Charts, SmartArt and embedded objects</b> are drawn as a "
            "labelled box — deliberately, rather than guessing wrong.",
        ])
    faithful_items = "".join(
        "<li>%s</li>" % s for s in [
            "<b>Theme colours</b>, resolved through the master's colour map "
            "and its tint/shade modifiers.",
            "<b>Master and layout</b> composited under the slide, with prompt "
            "placeholders suppressed.",
            "<b>Placeholder geometry</b> inherited from the layout when a "
            "slide declares none.",
            "<b>Tables and images</b>, drawn to their real geometry.",
        ])

    return """<title>Library Contact Sheet</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap">
<style>%(css)s</style>

<div class="wrap">
  <header class="masthead">
    <div>
      <p class="eyebrow">ppt_gen_v2 &middot; preview renderer</p>
      <h1>%(name)s library</h1>
      <p class="standfirst">Every block in <code>%(library)s</code>, drawn from its
      OOXML in-process — no LibreOffice, no PowerPoint, nothing installed. This is
      what the author console will show when someone names a block or writes a
      rule against it.</p>
    </div>
    <dl class="facts">
      <div class="fact"><dt>Blocks</dt><dd>%(blocks)d</dd></div>
      <div class="fact"><dt>Placeholders</dt><dd>%(slots)d</dd></div>
      <div class="fact"><dt>Payload</dt><dd>%(kb).0f<span> KB</span></dd></div>
      <div class="fact"><dt>Render</dt><dd>%(ms).0f<span> ms</span></dd></div>
    </dl>
  </header>

  <div class="controls">
    <button class="toggle" id="lit" type="button" aria-pressed="false">
      <span class="dot"></span><span class="txt">Highlight placeholders</span>
    </button>
    <span class="hint">Placeholder runs are tagged inside the SVG itself.
    Click any slide to enlarge.</span>
  </div>

  <div class="sheet" id="sheet">%(cells)s</div>

  <section class="notes">
    <h2>How to read this</h2>
    <p>A thumbnail answers <em>“which slide is this, and where do values land?”</em>
    It is not a brand proof. Judge it on whether you could pick a slide out of sixty
    — not on whether the kerning matches.%(unsupported)s</p>
    <div class="grid2">
      <div><h3>Rendered faithfully</h3><ul>%(faithful)s</ul></div>
      <div><h3>Approximated</h3><ul>%(caveats)s</ul></div>
    </div>
  </section>

  <footer>%(kb).0f KB of SVG for %(blocks)d slides &middot; equivalent PNGs would be
  roughly %(png)d&times; larger and need a %(lo)s install%(failed)s</footer>
</div>

<dialog id="zoom">
  <div id="stage"></div>
  <div class="dlg-bar"><span id="zoom-label"></span>
  <button id="close" type="button">Close</button></div>
</dialog>

<script>%(js)s</script>
""" % {
        "css": CSS, "js": JS,
        "name": _esc(template.name),
        "library": _esc(os.path.basename(template.library_path)),
        "blocks": len(results),
        "slots": len(slots),
        "kb": total_svg / 1024.0,
        "ms": results[0].get("_ms", 0) if results else 0,
        "cells": "".join(cells),
        "faithful": faithful_items,
        "caveats": caveat_items,
        "png": 12,
        "lo": "400&nbsp;MB LibreOffice",
        "unsupported": (" This template contains %s, shown as labelled boxes."
                        % ", ".join(unsupported)) if unsupported else "",
        "failed": (" &middot; %d slide(s) failed to render" % len(failed))
                  if failed else "",
    }


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("template", nargs="?", default="demo")
    ap.add_argument("-o", "--out", default=os.path.join(HERE, "out",
                                                        "contact_sheet.html"))
    ap.add_argument("--templates", default=os.path.join(HERE, "templates"))
    args = ap.parse_args(argv)

    import time
    tpl = (Template(args.template) if os.path.isdir(args.template)
           else find_template(args.templates, args.template))
    start = time.time()
    results = svgmod.render_template(tpl)
    elapsed = (time.time() - start) * 1000
    if results:
        results[0]["_ms"] = elapsed

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(build_page(tpl, results))
    print("wrote %s (%d blocks, %.0f ms to render)"
          % (args.out, len(results), elapsed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
