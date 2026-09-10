"""Render a deck to slide images. Four backends, one interface.

`engine/svg.py` draws an approximation from the OOXML: fast, dependency-free,
and honest about what it cannot draw. That is the right trade for a 128-slide
library grid, where the question is only *which slide is this*.

It is the wrong trade for "show me the deck I just built". There the question is
*what will the client see*, and an approximation that loses the corporate font,
the SmartArt and half the text answers a different question — it comes back
looking like a skeleton of the deck rather than the deck.

So we hand the file to something that renders it properly:

  **library** — pages copied out of a PDF of the library, converted once when
  the library was imported. Nothing runs at render time, on any platform, so
  this is the one that deploys. It cannot show filled values: the pages predate
  substitution, so the cover reads `{{ClientName}}` and the UI says so.

  **libreoffice** — `soffice --headless --convert-to pdf`, rasterised with
  pypdfium2. Runs anywhere, with no licensing question, and renders the built
  file so values *do* show. Kept for a deployment that is allowed to install it.

  **powerpoint** — COM automation on Windows. Exact, because it *is* PowerPoint.
  This is the deliberate final check before a deck goes out, and the thing that
  produces the library PDF in the first place. It cannot be deployed: Azure has
  no desktop session, and Microsoft's licensing does not permit server-side
  Office automation.

  **svg** — the in-process approximation. Always available, so there is always
  an answer, and it is labelled in the UI so nobody mistakes it for the deck.

Whichever draws the page, the caller is told which one did. A preview that
quietly degrades is a preview nobody can use as evidence.

Configuration, all optional:

    PPTGEN_RENDERER         auto (default) | library | libreoffice
                            | powerpoint | svg
    PPTGEN_SOFFICE          path to soffice, if it is somewhere unusual
    PPTGEN_RENDER_TIMEOUT   seconds before a conversion is abandoned (300)

Locking. Renders are keyed by output folder, and a build token names exact
bytes, so the lock that matters is per deck: ten people opening the same
proposal cost one conversion and nine cache reads. Different decks convert in
parallel under LibreOffice — each with its own profile directory, without which
a second soffice quietly hands its work to the first and returns having done
nothing. PowerPoint is single-instance per Windows session and takes an extra
global lock on top.
"""
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading

WIDTH = 1600                 # long edge, in pixels; a 16:9 slide is 1600x900
TIMEOUT = int(os.environ.get("PPTGEN_RENDER_TIMEOUT") or 300)
ORDER = ("libreoffice", "powerpoint")     # preference; svg is the caller's job
_DONE = "render.json"        # cache marker: what is in this folder, and of what

_COM_LOCK = threading.Lock()              # PowerPoint: one session, one export
_LOCKS = {}                               # out_dir -> lock, so a deck converts once
_LOCKS_GUARD = threading.Lock()


class RenderError(RuntimeError):
    """This backend could not draw this deck. The caller falls back."""


def _lock_for(key):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# ---------------------------------------------------------------- LibreOffice
SOFFICE_GUESSES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def soffice():
    """Where LibreOffice is, or None. PATH first — that is how a container has
    it — then the places an installer puts it on a desktop."""
    told = os.environ.get("PPTGEN_SOFFICE")
    if told:
        return told if os.path.exists(told) else None
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    if found:
        return found
    return next((p for p in SOFFICE_GUESSES if os.path.exists(p)), None)


def _probe_libreoffice():
    if soffice() is None:
        return False, ("LibreOffice is not installed, or soffice is not on PATH "
                       "(set PPTGEN_SOFFICE to point at it)")
    try:
        import pypdfium2                                          # noqa: F401
    except ImportError:
        return False, "pypdfium2 is not installed (pip install pypdfium2 pillow)"
    try:
        import PIL                                                # noqa: F401
    except ImportError:
        return False, "pillow is not installed (pip install pillow)"
    return True, None


def _to_pdf(pptx, work):
    """pptx -> pdf, in `work`. -> pdf path."""
    exe = soffice()
    profile = os.path.join(work, "profile")
    os.makedirs(profile, exist_ok=True)
    # A private profile per conversion. Without it a second soffice sees the
    # first one's running instance, hands the job over and exits 0 having
    # written nothing - which looks exactly like success.
    cmd = [exe, "--headless", "--norestore", "--invisible", "--nolockcheck",
           "-env:UserInstallation=%s" % _file_url(profile),
           "--convert-to", "pdf", "--outdir", work, pptx]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RenderError("LibreOffice gave up after %ds on %s"
                          % (TIMEOUT, os.path.basename(pptx)))
    pdf = os.path.join(work, os.path.splitext(os.path.basename(pptx))[0] + ".pdf")
    if not os.path.exists(pdf):
        detail = (done.stderr or done.stdout or b"").decode("utf-8", "replace")
        raise RenderError("LibreOffice wrote no PDF (exit %d) %s"
                          % (done.returncode, detail.strip()[:300]))
    return pdf


def _file_url(path):
    """A file:// URL LibreOffice accepts on both Windows and Linux."""
    from urllib.request import pathname2url
    return "file:///" + pathname2url(os.path.abspath(path)).lstrip("/")


def _rasterise(pdf, out_dir, width):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        paths = []
        for i in range(len(doc)):
            page = doc[i]
            scale = float(width) / max(float(page.get_width()), 1.0)
            path = os.path.join(out_dir, "slide-%03d.png" % (i + 1))
            page.render(scale=scale).to_pil().save(path)
            paths.append(path)
        return paths
    finally:
        doc.close()


def _export_libreoffice(pptx, out_dir, width):
    work = tempfile.mkdtemp(prefix="pptgen-render-")
    try:
        return _rasterise(_to_pdf(pptx, work), out_dir, width)
    except RenderError:
        raise
    except Exception as exc:
        raise RenderError("%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- library PDF
#
# The one that needs nothing at runtime.
#
# A deck is built by copying slides out of the library, so a preview can be
# built by copying pages out of a PDF *of* that library — converted once, when
# the library is imported, on a machine that has PowerPoint. After that there is
# no converter anywhere: assembling a preview is picking pages, which is pure
# Python and identical on a laptop and in a Linux container.
#
# What it cannot do is show filled values. The pages come from the library, so
# the cover reads `{{ClientName}}`. That is a real limit and the UI says so
# rather than letting anyone think their deck shipped that way.
#
# Page N of the PDF is slide N of the library, and nothing checks that for us —
# so `check_pdf` is called at import and the pairing is refused if the counts
# disagree. PowerPoint omits hidden slides from a PDF export, which would shift
# every page after the first hidden one and show the wrong slide with complete
# confidence.
def _probe_library():
    try:
        import pypdfium2                                          # noqa: F401
        import PIL                                                # noqa: F401
    except ImportError as exc:
        return False, "%s (pip install pypdfium2 pillow)" % exc
    return True, None


def page_count(pdf):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        return len(doc)
    finally:
        doc.close()


def check_pdf(pdf, slides):
    """-> (ok, why). Is this PDF a page-per-slide match for that library?"""
    ok, why = _probe_library()
    if not ok:
        return False, why
    try:
        pages = page_count(pdf)
    except Exception as exc:
        return False, "could not read the PDF: %s" % exc
    if pages != slides:
        return False, ("the PDF has %d page(s) and the library has %d slide(s). "
                       "They have to match one for one, or a preview shows the "
                       "wrong slide. Hidden slides are the usual cause — "
                       "PowerPoint leaves them out of a PDF export."
                       % (pages, slides))
    return True, None


def make_library_pdf(pptx, pdf):
    """Export a library to PDF with PowerPoint. Import-time and local only —
    this is the one step that still wants Windows, once per library."""
    ok, why = _probe_powerpoint()
    if not ok:
        raise RenderError(why)
    import pythoncom
    import win32com.client as com

    with _COM_LOCK:
        pythoncom.CoInitialize()
        app, ours = None, False
        try:
            try:
                app = com.GetActiveObject("PowerPoint.Application")
            except Exception:
                app = com.Dispatch("PowerPoint.Application")
                ours = True
            pres = app.Presentations.Open(os.path.abspath(pptx), ReadOnly=1,
                                          Untitled=0, WithWindow=0)
            try:
                pres.SaveAs(os.path.abspath(pdf), 32)      # ppSaveAsPDF
            finally:
                try:
                    pres.Close()
                except Exception:
                    pass
        except Exception as exc:
            raise RenderError("%s: %s" % (type(exc).__name__, exc))
        finally:
            if ours and app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()
    return pdf


def preview_from_library(pdf, pages, out_dir, width=WIDTH):
    """Rasterise `pages` (0-based, in deck order) of a library PDF.

    -> ("library", [png path, ...]). Cached like any other render.
    """
    pdf, out_dir = os.path.abspath(pdf), os.path.abspath(out_dir)
    if not os.path.exists(pdf):
        raise RenderError("this library has no PDF to preview from")
    ok, why = _probe_library()
    if not ok:
        raise RenderError(why)

    with _lock_for(out_dir):
        hit = _cached(out_dir, pdf, pages)
        if hit:
            return "library", hit
        _clear(out_dir)
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf)
        try:
            n = len(doc)
            wrong = [p for p in pages if not 0 <= p < n]
            if wrong:
                raise RenderError(
                    "the deck wants library page(s) %s and the PDF has %d — "
                    "it is out of date with the library"
                    % (", ".join(str(p + 1) for p in wrong[:5]), n))
            paths = []
            for i, page_no in enumerate(pages, 1):
                page = doc[page_no]
                scale = float(width) / max(float(page.get_width()), 1.0)
                path = os.path.join(out_dir, "slide-%03d.png" % i)
                page.render(scale=scale).to_pil().save(path)
                paths.append(path)
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError("%s: %s" % (type(exc).__name__, exc))
        finally:
            doc.close()
        _mark(out_dir, pdf, paths, "library", pages)
        return "library", paths


# ---------------------------------------------------------------- PowerPoint
def _probe_powerpoint():
    """Cheap: no PowerPoint is launched to answer it. Launching it takes
    seconds, and a probe that costs seconds gets called once and cached wrongly.
    """
    if platform.system() != "Windows":
        return False, ("PowerPoint automation needs Windows — this is %s"
                       % platform.system())
    try:
        import win32com.client                                    # noqa: F401
    except ImportError:
        return False, "pywin32 is not installed (pip install pywin32)"
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                       "PowerPoint.Application"))
    except OSError:
        return False, "PowerPoint is not installed on this machine"
    return True, None


def _export_powerpoint(pptx, out_dir, width):
    import pythoncom
    import win32com.client as com

    with _COM_LOCK:                        # one PowerPoint, one export at a time
        pythoncom.CoInitialize()
        app, ours = None, False
        try:
            try:
                app = com.GetActiveObject("PowerPoint.Application")
            except Exception:              # not running — start our own
                app = com.Dispatch("PowerPoint.Application")
                ours = True
            try:
                app.DisplayAlerts = 1      # ppAlertsNone: never block on a dialog
            except Exception:
                pass

            pres = app.Presentations.Open(pptx, ReadOnly=1, Untitled=0,
                                          WithWindow=0)
            try:
                slide_w = float(pres.PageSetup.SlideWidth or 960)
                slide_h = float(pres.PageSetup.SlideHeight or 540)
                px_w = int(width)
                px_h = max(1, int(round(px_w * slide_h / max(slide_w, 1))))
                paths = []
                for i in range(1, int(pres.Slides.Count) + 1):
                    path = os.path.join(out_dir, "slide-%03d.png" % i)
                    pres.Slides(i).Export(path, "PNG", px_w, px_h)
                    paths.append(path)
            finally:
                try:
                    pres.Close()
                except Exception:
                    pass
            return paths
        except Exception as exc:
            raise RenderError("%s: %s" % (type(exc).__name__, exc))
        finally:
            if ours and app is not None:
                try:
                    app.Quit()             # only ever the instance we started
                except Exception:
                    pass
            pythoncom.CoUninitialize()


BACKENDS = {"libreoffice": (_probe_libreoffice, _export_libreoffice),
            "powerpoint": (_probe_powerpoint, _export_powerpoint)}


# ---------------------------------------------------------------- choosing
def probe(name):
    """-> (usable, why not) for one backend."""
    if name == "svg":
        return True, None
    if name == "library":
        return _probe_library()
    if name not in BACKENDS:
        return False, "no such renderer: %s" % name
    return BACKENDS[name][0]()


def chosen(prefer=None):
    """Which renderer a call would use right now, and why not the better one.

    -> (name, why). `name` is "svg" when nothing else is available, and `why`
    then says what is missing, so the UI can tell someone what to install.
    """
    prefer = prefer or os.environ.get("PPTGEN_RENDERER") or "auto"
    if prefer != "auto":
        ok, why = probe(prefer)
        return (prefer, None) if ok else ("svg", why)
    first = None
    for name in ORDER:
        ok, why = probe(name)
        if ok:
            return name, None
        first = first or why
    return "svg", first


def engines():
    """Every backend and whether it is usable here — for /docs and diagnostics."""
    out = []
    for name in ("library",) + ORDER + ("svg",):
        ok, why = probe(name)
        out.append({"engine": name, "available": ok, "why": why})
    return out


# ---------------------------------------------------------------- the cache
def _cached(out_dir, source, pages=None):
    """Paths from a previous export, if they still describe this source.

    `pages` matters for the library backend: the same folder would otherwise
    serve a cached preview of a different slide selection after a library was
    re-imported and the pages moved.
    """
    try:
        with open(os.path.join(out_dir, _DONE), "r", encoding="utf-8") as fh:
            note = json.load(fh)
        if note.get("source_size") != os.path.getsize(source):
            return None
        if list(note.get("pages") or []) != list(pages or []):
            return None
        paths = [os.path.join(out_dir, n) for n in note.get("files", [])]
        return paths if paths and all(os.path.exists(p) for p in paths) else None
    except (OSError, ValueError):
        return None


def _mark(out_dir, source, paths, engine, pages=None):
    with open(os.path.join(out_dir, _DONE), "w", encoding="utf-8") as fh:
        json.dump({"source_size": os.path.getsize(source), "engine": engine,
                   "pages": list(pages or []),
                   "files": [os.path.basename(p) for p in paths]}, fh)


def _clear(out_dir):
    """Drop a half-finished earlier attempt before writing over it."""
    os.makedirs(out_dir, exist_ok=True)
    for leftover in os.listdir(out_dir):
        if leftover.endswith(".png") or leftover == _DONE:
            try:
                os.remove(os.path.join(out_dir, leftover))
            except OSError:
                pass


def cached_engine(out_dir):
    """Which backend drew what is already in `out_dir`, if anything."""
    try:
        with open(os.path.join(out_dir, _DONE), "r", encoding="utf-8") as fh:
            return json.load(fh).get("engine")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- the door
def deck_to_images(pptx, out_dir, width=WIDTH, backend=None):
    """Export every slide of `pptx` as a PNG in `out_dir`. -> (engine, [paths])

    Cached: a second call for the same file returns the same paths without
    converting again. Raises RenderError if the chosen backend cannot run.
    """
    pptx = os.path.abspath(pptx)
    out_dir = os.path.abspath(out_dir)
    if not os.path.exists(pptx):
        raise RenderError("no such deck: %s" % pptx)

    name, why = chosen(backend)
    if name == "svg":
        raise RenderError(why or "no image renderer is available here")

    with _lock_for(out_dir):               # this deck converts once, not per viewer
        hit = _cached(out_dir, pptx)
        if hit:
            return cached_engine(out_dir) or name, hit
        _clear(out_dir)
        paths = BACKENDS[name][1](pptx, out_dir, width)
        if not paths:
            raise RenderError("%s exported nothing" % name)
        _mark(out_dir, pptx, paths, name)
        return name, paths
