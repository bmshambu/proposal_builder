"""A *template* is a folder. Adding one is a file operation, never a code change.

    templates/<name>/
      template.json       what this template is (name, description, library file)
      library.pptx        the deck someone authored in PowerPoint
      blocks.json         slide -> block id, so an unedited deck can be named
      rules.json          which blocks a given answer set produces
      data_sources.json   stubs for external systems (optional)

`import_deck()` turns any `.pptx` into that folder: it reads the deck, derives a
block id per slide from its title, writes the sidecar and a starter `rules.json`
whose baseline is every slide in order. The result builds immediately — a
faithful copy of the source deck — and is then narrowed by editing rules.json.

That is the whole "add a new template" story, and it is why the engine never
needs to know a template exists: it takes paths.
"""
import json
import os
import shutil
import zipfile

from .library import (Library, LibraryError, _entry_id,
                      normalise_block_map)

MANIFEST = "template.json"
LIBRARY = "library.pptx"
BLOCKS = "blocks.json"
RULES = "rules.json"
DATA = "data_sources.json"


class TemplateError(Exception):
    pass


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


class Template:
    """One template folder, loaded lazily."""

    def __init__(self, folder):
        self.folder = os.path.abspath(str(folder))
        if not os.path.isdir(self.folder):
            raise TemplateError("no such template folder: %s" % self.folder)
        self.manifest = _read_json(os.path.join(self.folder, MANIFEST), {}) or {}
        self.name = self.manifest.get("name") or os.path.basename(self.folder)
        self.library_path = os.path.join(self.folder,
                                         self.manifest.get("library", LIBRARY))
        if not os.path.exists(self.library_path):
            found = _sole_pptx(self.folder)
            if not found:
                raise TemplateError(
                    "template %r has no library deck (expected %s)" % (self.name, LIBRARY))
            self.library_path = found
        self.blocks_path = os.path.join(self.folder, BLOCKS)
        self.rules_path = os.path.join(self.folder, RULES)
        self.data_path = os.path.join(self.folder, DATA)

    # -- parts ------------------------------------------------------------
    @property
    def block_map(self):
        raw = _read_json(self.blocks_path, {}) or {}
        return normalise_block_map(raw.get("blocks", raw))

    @property
    def raw_block_map(self):
        raw = _read_json(self.blocks_path, {}) or {}
        return raw.get("blocks", raw)

    def open_library(self, strict_ids=False):
        # the raw map, so the library can also report drift against the
        # titles recorded at import time
        return Library(self.library_path, block_map=self.raw_block_map,
                       strict_ids=strict_ids)

    def load_rules(self):
        from .rules import Rules
        return Rules.load(self.rules_path)

    def data_sources(self):
        return _read_json(self.data_path, None)

    def answer_sets(self):
        """Any answers*.json sitting in the folder — handy defaults for the CLI."""
        return sorted(f for f in os.listdir(self.folder)
                      if f.startswith("answers") and f.endswith(".json"))

    def describe(self):
        try:
            with self.open_library() as lib:
                blocks, loose = len(lib.blocks), sum(1 for b in lib.blocks.values()
                                                     if not b.marked)
        except LibraryError as exc:
            return {"name": self.name, "folder": self.folder, "error": str(exc)}
        return {"name": self.name, "folder": self.folder,
                "description": self.manifest.get("description", ""),
                "library": os.path.basename(self.library_path),
                "blocks": blocks, "unnamed": loose,
                "has_rules": os.path.exists(self.rules_path),
                "answer_sets": self.answer_sets()}

    # -- maintenance ------------------------------------------------------
    def write_block_map(self, mapping):
        _write_json(self.blocks_path, {
            "_comment": "Slide -> block id. Edit the ids freely; they are what "
                        "rules.json refers to. A {{block:id}} marker on the slide "
                        "itself always wins over this file.",
            "blocks": mapping})
        return self.blocks_path

    def reconcile_blocks(self):
        """Re-key `blocks.json` against the library as it is now.

        The sidecar maps a slide *part name* to a block id, so anything that
        renumbers parts silently breaks it — a designer reordering the deck, or
        PowerPoint rewriting the package during a repair. Nothing errors: the
        ids simply start naming the wrong slides, and every rule quietly pulls
        the wrong content. That is the worst kind of wrong, so it is worth a
        deliberate step rather than a hope.

        Matching is by the title recorded when the id was assigned, since that
        is what the sidecar carries. -> report dict; nothing is written unless
        something actually moved.
        """
        raw = self.raw_block_map
        if not raw:
            return {"moved": [], "unmatched": [], "vanished": [], "changed": False}

        with self.open_library() as lib:
            current = [(os.path.basename(b.part), b.title) for b in lib.ordered()]

        def entry_title(e):
            return e.get("title") if isinstance(e, dict) else None

        taken, moved, unmatched = set(), [], []
        resolved = {}
        # 1. a part that still exists and still has its recorded title is settled
        by_part = dict(current)
        for part, e in raw.items():
            if part in by_part and entry_title(e) == by_part[part]:
                resolved[part] = e
                taken.add(part)
        # 2. anything left is matched on its recorded title, in deck order
        for part, e in raw.items():
            if part in resolved:
                continue
            title = entry_title(e)
            hit = next((p for p, t in current
                        if p not in taken and title and t == title), None)
            if hit:
                resolved[hit] = e
                taken.add(hit)
                moved.append((_entry_id(e), part, hit))
            else:
                unmatched.append((_entry_id(e), part))

        vanished = [p for p, _t in current if p not in taken]
        changed = bool(moved)
        if changed:
            self.write_block_map(resolved)
        return {"moved": moved, "unmatched": unmatched, "vanished": vanished,
                "changed": changed}

    def rename_block(self, old, new):
        """Rename a block in the sidecar, keeping rules.json in step."""
        raw = self.raw_block_map
        part = next((p for p, e in raw.items()
                     if (e.get("id") if isinstance(e, dict) else e) == old), None)
        if part is None:
            raise TemplateError("no block %r in %s" % (old, BLOCKS))
        if new in normalise_block_map(raw).values():
            raise TemplateError("block id %r is already taken" % new)
        entry = raw[part]
        raw[part] = dict(entry, id=new) if isinstance(entry, dict) else new
        self.write_block_map(raw)
        if os.path.exists(self.rules_path):
            text = open(self.rules_path, encoding="utf-8").read()
            open(self.rules_path, "w", encoding="utf-8").write(
                text.replace('"%s"' % old, '"%s"' % new))
        return part


# ---------------------------------------------------------------- discovery
def _sole_pptx(folder):
    decks = [f for f in os.listdir(folder)
             if f.lower().endswith(".pptx") and not f.startswith("~$")]
    return os.path.join(folder, decks[0]) if len(decks) == 1 else None


def list_templates(root):
    """Every template folder under `root`, by name."""
    root = os.path.abspath(str(root))
    if not os.path.isdir(root):
        return []
    out = []
    for entry in sorted(os.listdir(root)):
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder):
            continue
        if os.path.exists(os.path.join(folder, MANIFEST)) or _sole_pptx(folder) \
                or os.path.exists(os.path.join(folder, LIBRARY)):
            try:
                out.append(Template(folder))
            except TemplateError:
                continue
    return out


def find_template(root, name_or_path):
    """Accept a template name, a folder path, or a bare .pptx."""
    if os.path.isdir(name_or_path):
        return Template(name_or_path)
    candidate = os.path.join(str(root), str(name_or_path))
    if os.path.isdir(candidate):
        return Template(candidate)
    raise TemplateError(
        "no template %r (looked in %s). Known: %s"
        % (name_or_path, root,
           ", ".join(t.name for t in list_templates(root)) or "none"))


# ---------------------------------------------------------------- import
def import_deck(src_pptx, root, name=None, description="", overwrite=False,
                keep_source_name=False):
    """Turn any `.pptx` into a template folder. Returns a report dict.

    Nothing about the deck is modified — it is copied verbatim. Identity comes
    from the sidecar this writes, so an existing firm template works untouched.
    """
    src_pptx = str(src_pptx)
    if not os.path.exists(src_pptx):
        raise TemplateError("no such deck: %s" % src_pptx)
    if not zipfile.is_zipfile(src_pptx):
        raise TemplateError("%s is not a .pptx (not a zip archive)" % src_pptx)

    stem = os.path.splitext(os.path.basename(src_pptx))[0]
    from .library import slugify
    name = name or slugify(stem, fallback="template")
    folder = os.path.join(str(root), name)
    if os.path.exists(folder):
        if not overwrite:
            raise TemplateError(
                "template %r already exists at %s — pass overwrite to replace it"
                % (name, folder))
        shutil.rmtree(folder)
    os.makedirs(folder)

    library_name = (os.path.basename(src_pptx) if keep_source_name else LIBRARY)
    shutil.copy(src_pptx, os.path.join(folder, library_name))

    try:
        with Library(os.path.join(folder, library_name)) as lib:
            mapping = lib.suggest_block_map()
            summary = lib.summary()
            found_placeholders = sorted(lib.all_placeholders())
            markers = sum(1 for b in lib.blocks.values() if b.source == "marker")
            guessed = sum(1 for b in lib.blocks.values() if b.source == "title")
            positional = sum(1 for b in lib.blocks.values() if b.source == "position")
    except LibraryError:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    tpl = Template(folder)
    tpl.write_block_map(mapping)

    _write_json(os.path.join(folder, MANIFEST), {
        "name": name,
        "description": description or ("Imported from %s" % os.path.basename(src_pptx)),
        "library": library_name,
        "source_deck": os.path.basename(src_pptx),
    })

    order = [e["id"] for e in mapping.values()]
    _write_json(os.path.join(folder, RULES), {
        "name": name,
        "library": library_name,
        "_comment": "Starter rules: every slide, in the deck's own order. Narrow it "
                    "by moving optional blocks out of `baseline` into `blocks` with a "
                    "`when` and an `insert_after`. See docs/authoring-a-library.md.",
        "baseline": order,
        "blocks": {},
        "placeholders": {("{{%s}}" % p): {"from": "field", "field": p}
                         for p in found_placeholders},
    })

    return {"name": name, "folder": folder,
            "library": os.path.join(folder, library_name),
            "slides": len(mapping), "blocks": summary,
            "ids_from_markers": markers, "ids_from_titles": guessed,
            "ids_from_position": positional,
            "placeholders": found_placeholders}
