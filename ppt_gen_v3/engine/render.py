"""Render a deck the way PowerPoint does — by asking PowerPoint.

`engine/svg.py` draws an approximation from the OOXML: fast, dependency-free,
and honest about what it cannot draw. That is the right trade for a 128-slide
library grid, where the question is only *which slide is this*.

It is the wrong trade for "show me the deck I just built". There the question is
*what will the client see*, and an approximation that loses the corporate font,
the SmartArt and half the text answers a different question — it looks like a
skeleton of the deck rather than the deck.

So when PowerPoint is on the machine, we drive it over COM and export real
slide images. Every machine with a reason to run this console has PowerPoint on
it. When it is missing, the SVG renderer still answers and the UI says plainly
which one drew the page, because a preview nobody can trust is worse than no
preview.

Two rules this module will not break:

  **It never closes a presentation it did not open.** COM attaches to whatever
  PowerPoint is already running, so quitting on the way out would shut the
  author's own unsaved work. We quit only an instance we started ourselves, and
  we always close our own presentation.

  **One export at a time.** A single COM instance driven from two request
  threads fails in ways that are tedious to reproduce, so exports queue on a
  lock.

Renders are cached per deck under `data/renders/<token>/`. A built deck never
changes — the token names its exact bytes — so the second look at a deck is a
static file read.
"""
import json
import os
import platform
import threading

WIDTH = 1600                 # long edge, in pixels; a 16:9 slide is 1600x900
_LOCK = threading.Lock()
_DONE = "render.json"        # cache marker: what is in this folder, and of what


class RenderError(RuntimeError):
    """PowerPoint could not draw this deck. The caller falls back to SVG."""


# ---------------------------------------------------------------- probing
def available():
    """-> (usable, why not). Cheap: no PowerPoint is launched to answer it.

    Launching PowerPoint takes seconds, and a probe that costs seconds gets
    called once and cached wrongly. Asking the registry costs nothing.
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


def engine_name():
    """Which renderer a call would use right now, and why."""
    ok, why = available()
    return ("powerpoint", None) if ok else ("svg", why)


# ---------------------------------------------------------------- the cache
def _cached(out_dir, source):
    """Paths from a previous export, if they still describe this file."""
    marker = os.path.join(out_dir, _DONE)
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            note = json.load(fh)
        if note.get("source_size") != os.path.getsize(source):
            return None
        paths = [os.path.join(out_dir, n) for n in note.get("files", [])]
        return paths if paths and all(os.path.exists(p) for p in paths) else None
    except (OSError, ValueError):
        return None


def _mark(out_dir, source, paths):
    with open(os.path.join(out_dir, _DONE), "w", encoding="utf-8") as fh:
        json.dump({"source_size": os.path.getsize(source),
                   "files": [os.path.basename(p) for p in paths]}, fh)


# ---------------------------------------------------------------- exporting
def _export(pptx, out_dir, width):
    """Drive PowerPoint. -> [png path, ...] in slide order."""
    import pythoncom
    import win32com.client as com

    pythoncom.CoInitialize()
    app, ours = None, False
    try:
        try:
            app = com.GetActiveObject("PowerPoint.Application")
        except Exception:                      # not running — start our own
            app = com.Dispatch("PowerPoint.Application")
            ours = True
        try:
            app.DisplayAlerts = 1              # ppAlertsNone: never block on a dialog
        except Exception:
            pass

        pres = app.Presentations.Open(pptx, ReadOnly=1, Untitled=0, WithWindow=0)
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
                app.Quit()                     # only ever the instance we started
            except Exception:
                pass
        pythoncom.CoUninitialize()


def deck_to_images(pptx, out_dir, width=WIDTH):
    """Export every slide of `pptx` as a PNG in `out_dir`. -> [path, ...]

    Cached: a second call for the same file returns the same paths without
    opening PowerPoint again.
    """
    pptx = os.path.abspath(pptx)
    out_dir = os.path.abspath(out_dir)
    if not os.path.exists(pptx):
        raise RenderError("no such deck: %s" % pptx)
    ok, why = available()
    if not ok:
        raise RenderError(why)

    with _LOCK:
        hit = _cached(out_dir, pptx)
        if hit:
            return hit
        os.makedirs(out_dir, exist_ok=True)
        for name in os.listdir(out_dir):       # a half-finished earlier attempt
            if name.endswith(".png") or name == _DONE:
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass
        paths = _export(pptx, out_dir, width)
        if not paths:
            raise RenderError("PowerPoint exported nothing")
        _mark(out_dir, pptx, paths)
        return paths
