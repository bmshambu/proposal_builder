#!/usr/bin/env python3
"""Photograph the build screen, so layout can be looked at rather than assumed.

    python tools/shoot_ui.py              -> tools/shots/wide.png, narrow.png

Every other check here asserts something a browser cannot disagree with: an
element exists, a class is set, a contrast ratio clears 4.5. None of them can
see that the preview ended up below the fold at the size of the window, which is
what actually happened - and the class assertion for it passed the whole time.

So this lifts the real markup out of index.html, puts it in the state the page
reaches after a build, fills it with real rendered slides and photographs it in
headless Chrome at two widths. It prints nothing but paths: **it is for looking
at**, not a pass/fail. It found a grid item refusing to shrink below its
filmstrip, which pushed the page past the window at 1000px.

Skips cleanly when Chrome is missing or nothing has been built yet.
"""
import base64
import glob
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "web")
OUT = os.path.join(HERE, "shots")
os.makedirs(OUT, exist_ok=True)

CHROMES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
)
CHROME = (shutil.which("chrome") or shutil.which("google-chrome")
          or next((c for c in CHROMES if os.path.exists(c)), None))
if not CHROME:
    sys.exit("no Chrome or Edge found - nothing to photograph with")

html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
css = open(os.path.join(WEB, "app.css"), encoding="utf-8").read()

section = re.search(r'<section class="screen" id="screen-build">.*?</section>',
                    html, re.S).group(0)

# the state the page is in after a build
section = section.replace('class="screen" id="screen-build"',
                          'class="screen on" id="screen-build"')
section = section.replace('class="cols build" id="build-cols"',
                          'class="cols build with-deck" id="build-cols"')
section = section.replace('<div class="panel" id="viewer-panel" hidden>',
                          '<div class="panel" id="viewer-panel">')
section = section.replace('<p class="viewer-note" id="viewer-note" hidden>',
                          '<p class="viewer-note" id="viewer-note">')


def uri(path):
    return "data:image/png;base64,%s" % base64.b64encode(
        open(path, "rb").read()).decode()


renders = glob.glob(os.path.join(ROOT, "data", "renders", "*"))
if not renders:
    sys.exit("nothing in data/renders - build a deck first, then run this")
newest = max(renders, key=os.path.getmtime)
pngs = sorted(glob.glob(os.path.join(newest, "*.png")))
if not pngs:
    sys.exit("no rendered slides in %s - build a deck first" % newest)

stage = '<img class="slide" src="%s">' % uri(pngs[0])
strip = "".join(
    '<button class="frame %s"><span class="fno">%d</span>'
    '<img class="fthumb" src="%s"></button>' % ("on" if i == 0 else "", i + 1, uri(p))
    for i, p in enumerate(pngs))

section = section.replace('<div class="stage" id="viewer-stage"></div>',
                          '<div class="stage" id="viewer-stage">%s</div>' % stage)
section = section.replace('<div class="filmstrip" id="viewer-strip"></div>',
                          '<div class="filmstrip" id="viewer-strip">%s</div>' % strip)
section = section.replace(
    '<p class="viewer-note" id="viewer-note">',
    '<p class="viewer-note" id="viewer-note"><b>*</b> Placeholders show '
    'unfilled here. These are your library&rsquo;s own slides, so this confirms '
    '<b>which slides and in what order</b> &mdash; the filled values are in the '
    'downloaded .pptx, and on the Values screen.')

# plausible content in the two left-hand panels, so the columns have real weight
form = "".join(
    '<div class="q"><span class="qt">%s</span><input type="text" value="%s"></div>'
    % (q, v) for q, v in [("Client name", "Northwind Manufacturing plc"),
                          ("Audit type", "New Audit Client"),
                          ("Due date", "2026-11-30"),
                          ("Office", "New York"),
                          ("Lead partner", "A. Partner")])
section = section.replace('<div class="pad" id="answers-form"></div>',
                          '<div class="pad" id="answers-form">'
                          '<div class="formgrid">%s</div></div>' % form)
rows = "".join('<div class="bl"><span class="n">%d</span>'
               '<span class="t">%s</span></div>' % (i + 1, t)
               for i, t in enumerate(
    ["Cover", "About us", "Your engagement team", "Our approach",
     "Scope of work", "Proposed fees", "Contacts"]))
section = section.replace('<div class="buildlist" id="build-list"></div>',
                          '<div class="buildlist" id="build-list">%s</div>' % rows)
section = section.replace('<span class="count" id="viewer-count"></span>',
                          '<span class="count" id="viewer-count">(1 of %d)</span>'
                          % len(pngs))
section = section.replace('<span class="count" id="build-count"></span>',
                          '<span class="count" id="build-count">(7 slides)</span>')
section = section.replace('id="viewer-how">rendered from the file itself\n        &middot; arrow keys to move',
                          'id="viewer-how">rendered from your library &middot; arrow keys to move')

page = ("<!doctype html><html><head><meta charset='utf-8'><style>%s</style>"
        "</head><body><main>%s</main></body></html>"
        % (css, section))
path = os.path.join(OUT, "build.html")
open(path, "w", encoding="utf-8").write(page)

for name, size in (("wide", "1600,1000"), ("narrow", "1000,900")):
    shot = os.path.join(OUT, "%s.png" % name)
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--screenshot=%s" % shot, "--window-size=%s" % size,
                    "--virtual-time-budget=3000", path],
                   capture_output=True, timeout=120)
    print(name, size, os.path.exists(shot) and os.path.getsize(shot))
print(OUT)


# not asserted, deliberately - open them
