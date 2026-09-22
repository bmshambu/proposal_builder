"""Saving an author's markings as a **new** library.

The original `library.pptx` is never written to. An author who marks up a
library gets a new one beside it, and the one they started from is still there,
byte for byte, to go back to. That is the whole recovery story: no backup file
to find, no restore to get right, no window where a half-written zip is the
only copy anyone has.

It also keeps this cheap. Saving is not a mutation to be made atomic - it is
building a file and handing it to `import_deck`, which is the path every
library already comes in by.

What carries across, and why:

  * **the rules**, because the block ids are carried too (see `save_as`), so
    every condition still resolves. Not carrying them would mean re-authoring
    every condition, which is the hardest screen in the tool and the one an
    author is least able to redo.
  * **the block ids**, as the sidecar. A firm template has no `{{block:id}}`
    markers in it - its ids live in `blocks.json`, derived from the titles at
    import - so anything that re-derives them turns a marked heading into a
    different block.
  * **the bindings**, with the new ones merged in. A placeholder and the thing
    it is bound to are written together or not at all; a token with no binding
    is a gap somebody has to notice later.

What does not carry is the library PDF: the slides have changed, so the old one
would preview the old words. The caller makes a new one - see `app.py`.
"""
import io
import json
import os
import shutil
import tempfile
import zipfile

from . import placeholders
from .template import find_template, import_deck


class MarkError(Exception):
    """The marks could not be written."""


def apply_marks(pptx, marks):
    """-> the bytes of `pptx` with every mark written into it.

    Every part that is not being marked is copied across unread and unchanged,
    with its own compression and timestamp, so the only difference between the
    two files is the text an author pointed at.
    """
    by_part = {}
    for mark in marks:
        by_part.setdefault(mark["part"], []).append(mark)

    buf = io.BytesIO()
    with zipfile.ZipFile(pptx) as src:
        names = set(src.namelist())
        astray = sorted(p for p in by_part if p not in names)
        if astray:
            raise MarkError("this library has no %s - it has changed since it "
                            "was read" % astray[0])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename in by_part:
                    try:
                        xml = placeholders.put(data.decode("utf-8"),
                                               by_part[info.filename])
                    except placeholders.PlaceholderError as exc:
                        raise MarkError("%s: %s"
                                        % (os.path.basename(info.filename), exc))
                    data = xml.encode("utf-8")
                dst.writestr(info, data)
    return buf.getvalue()


def carry_rules(src_folder, dst_folder, bindings=None):
    """Copy the rules over, with the new bindings merged in. -> [what changed]

    Nothing is invented: a placeholder the author did not mark keeps whatever
    binding it had, and a mark whose placeholder already existed does not
    overwrite it. Quietly rebinding something an author set up on the Values
    screen would be the kind of help nobody asked for.
    """
    src = os.path.join(src_folder, "rules.json")
    if not os.path.exists(src):
        return []
    with open(src, "r", encoding="utf-8") as fh:
        rules = json.load(fh)

    notes = []
    slots = dict(rules.get("placeholders") or {})
    for name, binding in (bindings or {}).items():
        key = "{{%s}}" % name
        if key in slots and slots[key]:
            notes.append("%s was already bound; left as it was" % key)
            continue
        slots[key] = binding
        notes.append("%s bound to %s" % (key, binding.get("from", "field")))
    rules["placeholders"] = slots

    with open(os.path.join(dst_folder, "rules.json"), "w",
              encoding="utf-8") as fh:
        json.dump(rules, fh, indent=2)
        fh.write("\n")
    return notes


def save_as(template, root, name, marks, bindings=None, description=""):
    """Write `marks` into a copy of `template` and import it as `name`.

    -> {"library", "folder", "slides", "marks", "notes"}

    Raises before anything is created if the marks do not fit the library, so a
    bad request leaves no half-made library behind.
    """
    data = apply_marks(template.library_path, marks)

    work = tempfile.mkdtemp(prefix="pptgen-mark-")
    try:
        staged = os.path.join(work, "library.pptx")
        with open(staged, "wb") as fh:
            fh.write(data)
        # The sidecar goes with it. `apply_marks` copies every part under its
        # own name, so the slide -> id mapping still lines up, and without it
        # the import guesses the ids again from the titles - which a mark on a
        # heading has just changed. Every rule then names a block that is no
        # longer there. It fails silently: the new library opens, the rules
        # load, and the deck simply comes out missing those slides.
        report = import_deck(staged, root, name=name, description=description,
                             block_map=template.raw_block_map)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    fresh = find_template(root, name)
    notes = carry_rules(template.folder, fresh.folder, bindings)
    return {"library": name, "folder": fresh.folder,
            "slides": report.get("slides") if isinstance(report, dict) else None,
            "marks": len(marks), "notes": notes}
