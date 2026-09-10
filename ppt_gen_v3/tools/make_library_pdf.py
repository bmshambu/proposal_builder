#!/usr/bin/env python3
"""Turn a library .pptx into the library .pdf the deck viewer previews from.

    python tools/make_library_pdf.py library.pptx
    python tools/make_library_pdf.py templates/firm -o firm.pdf

Run this once per library, on a machine with PowerPoint, then upload **both**
files on the Slides screen. After that nothing converts anything: a deck is
assembled by copying slides out of the library, so its preview is assembled by
copying pages out of this PDF — pure Python, identical on a laptop and in a
Linux container, no Office and no third-party renderer anywhere near the
deployment.

That only holds while **page N is slide N**, which is why this script checks the
count rather than trusting it, and warns about hidden slides first: PowerPoint
leaves them out of a PDF export, and one hidden slide shifts every page after it
so the viewer shows the wrong slide with complete confidence.

Deliberately a script and not a button. Converting needs PowerPoint; wiring it
into import would put that dependency straight back into the thing that has to
run without it.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import Template                                       # noqa: E402
from engine.library import Library                                # noqa: E402
from engine import render                                         # noqa: E402

HIDDEN_RE = re.compile(r'<p:sld\b[^>]*\bshow="0"')


def library_of(target):
    """A .pptx path, or a template folder holding one."""
    if os.path.isdir(target):
        return Template(target).library_path
    return target


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("library", help="library .pptx, or a template folder")
    ap.add_argument("-o", "--out", help="where to write (default: alongside, "
                                        "named library.pdf)")
    ap.add_argument("--force", action="store_true",
                    help="convert even if slides are hidden (the mapping will "
                         "be wrong — for looking, not for uploading)")
    args = ap.parse_args()

    pptx = library_of(args.library)
    if not os.path.exists(pptx):
        sys.exit("no such file: %s" % pptx)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(pptx)),
                                   "library.pdf")

    with Library(pptx) as lib:
        blocks = lib.ordered()
        hidden = [b for b in blocks if HIDDEN_RE.search(lib.read(b.part))]

    print("%s - %d slide(s)" % (os.path.basename(pptx), len(blocks)))
    if hidden:
        print()
        print("  %d slide(s) are hidden and PowerPoint will leave them out:"
              % len(hidden))
        for b in hidden:
            print("    slide %-3d %s" % (b.index + 1, b.id))
        print()
        print("  Every page after the first of these would line up with the")
        print("  wrong slide. Unhide them, or take them out of the library.")
        if not args.force:
            sys.exit(1)
        print("  --force given: converting anyway. Do not upload this.")

    ok, why = render.probe("powerpoint")
    if not ok:
        sys.exit("cannot convert here: %s\n"
                 "Run this on a machine with PowerPoint, or export the deck by "
                 "hand:\n  File > Save As > PDF, all slides." % why)

    print("converting with PowerPoint...")
    try:
        render.make_library_pdf(pptx, out)
    except render.RenderError as exc:
        sys.exit("PowerPoint could not export it: %s" % exc)

    ok, why = render.check_pdf(out, len(blocks))
    print()
    if not ok:
        print("MISMATCH - do not upload this PDF")
        print("  %s" % why)
        sys.exit(1)

    print("wrote %s" % out)
    print("  %d page(s), one per slide" % len(blocks))
    print()
    print("  page  block")
    for b in blocks:
        print("  %4d  %s" % (b.index + 1, b.id))
    print()
    print("Upload both files on the Slides screen: the .pptx and this .pdf.")


if __name__ == "__main__":
    main()
