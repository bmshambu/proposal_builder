#!/usr/bin/env python3
"""Regression tests for the v2 engine. Standard library only:

    python tests/test_engine.py

Each test that guards a v1 scar says which one, so nobody "simplifies" the fix
back out. Run this before every commit that touches engine/.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from engine import (Library, Rules, Template, build,          # noqa: E402
                    build_template, find_template, import_deck, list_templates)
from engine import bindings, ooxml as ooxmlmod, placeholders  # noqa: E402
from engine import report as reportmod                        # noqa: E402
from engine import svg as svgmod                              # noqa: E402
from engine import tokenise as tokenisemod                    # noqa: E402
from engine.assemble import AssemblyError                     # noqa: E402
from engine.library import LibraryError                       # noqa: E402
from engine.rules import RulesError, evaluate                 # noqa: E402
from engine.template import TemplateError                     # noqa: E402

DEMO = os.path.join(ROOT, "templates", "demo")
LIBRARY = os.path.join(DEMO, "library.pptx")
A_T = re.compile(r'<a:t(?:\s[^>]*)?>(.*?)</a:t>', re.DOTALL)


def _validator():
    path = os.path.join(os.path.dirname(ROOT), "validate_pptx.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("validate_pptx", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _json(name):
    with open(os.path.join(DEMO, name), encoding="utf-8") as fh:
        return json.load(fh)


def _slide_parts(zf):
    return sorted((n for n in zf.namelist()
                   if re.match(r'ppt/slides/slide\d+\.xml$', n)),
                  key=lambda n: int(re.search(r'\d+', os.path.basename(n)).group()))


def _deck_text(path):
    zf = zipfile.ZipFile(path)
    return "\n".join(" ".join(A_T.findall(zf.read(n).decode("utf-8")))
                     for n in _slide_parts(zf))


class EngineTestCase(unittest.TestCase):
    """Builds land in a temp dir so a failing run leaves nothing behind."""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(LIBRARY):
            sys.path.insert(0, os.path.join(ROOT, "tools"))
            from make_demo_library import write_library
            write_library(LIBRARY)
        cls.tmp = tempfile.mkdtemp(prefix="pptgen2_")
        cls.rules = Rules.load(os.path.join(DEMO, "rules.json"))
        cls.data = _json("data_sources.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def out(self, name):
        return os.path.join(self.tmp, name)

    def build(self, answers_file, name, **kw):
        return build(LIBRARY, self.rules, _json(answers_file), self.out(name),
                     data_sources=self.data, **kw)


# ---------------------------------------------------------------- library
class TestLibrary(EngineTestCase):
    def test_every_slide_is_a_marked_block(self):
        with Library(LIBRARY) as lib:
            self.assertEqual(len(lib.blocks), 9)
            unmarked = [b.id for b in lib.blocks.values() if not b.marked]
            self.assertEqual(unmarked, [], "every demo slide must carry a marker")

    def test_blocks_are_indexed_in_presentation_order(self):
        with Library(LIBRARY) as lib:
            self.assertEqual([b["id"] for b in lib.summary()][:3],
                             ["cover", "about_us", "our_team"])

    def test_split_placeholder_is_still_discovered(self):
        """`{{City}}` is authored across four runs on the contacts slide —
        the risk flagged in v2-plan §8. It must still be found."""
        with Library(LIBRARY) as lib:
            self.assertIn("City", lib.block("contacts").placeholders)

    def test_missing_block_names_what_is_available(self):
        with Library(LIBRARY) as lib:
            with self.assertRaises(LibraryError) as cm:
                lib.block("nope")
            self.assertIn("cover", str(cm.exception))


# ---------------------------------------------------------------- rules
class TestRules(EngineTestCase):
    def test_condition_operators(self):
        a = {"AuditType": "Expansion of Services", "Year": 2026, "Blank": ""}
        self.assertTrue(evaluate("always", a))
        self.assertFalse(evaluate("never", a))
        self.assertTrue(evaluate({"field": "AuditType", "eq": "Expansion of Services"}, a))
        self.assertTrue(evaluate({"field": "Year", "eq": "2026"}, a), "compares as strings")
        self.assertTrue(evaluate({"field": "AuditType", "in": ["Statutory Audit",
                                                               "Expansion of Services"]}, a))
        self.assertTrue(evaluate({"field": "AuditType", "ne": "Statutory Audit"}, a))
        self.assertTrue(evaluate({"field": "Blank", "exists": False}, a))
        self.assertTrue(evaluate({"all": [{"field": "Year", "eq": 2026},
                                          {"field": "AuditType", "exists": True}]}, a))
        self.assertTrue(evaluate({"any": [{"field": "Year", "eq": 1999},
                                          {"field": "AuditType", "exists": True}]}, a))
        self.assertTrue(evaluate({"not": {"field": "Year", "eq": 1999}}, a))

    def test_condition_matching_tolerates_case_and_spacing(self):
        """Answers come from forms; a rule must not silently miss on a stray
        space or capital."""
        a = {"AuditType": "  expansion   of services "}
        self.assertTrue(evaluate({"field": "AuditType", "eq": "Expansion of Services"}, a))

    def test_condition_without_operator_is_an_error(self):
        with self.assertRaises(RulesError):
            evaluate({"field": "AuditType"}, {})

    def test_baseline_selection(self):
        order, trace = self.rules.select(_json("answers.baseline.json"))
        self.assertEqual([b for b, _ in order],
                         ["cover", "about_us", "our_team", "approach",
                          "scope", "fees", "contacts"])
        self.assertEqual(trace, [], "baseline answers trigger no rules")

    def test_conditional_block_lands_at_its_anchor(self):
        order, _ = self.rules.select(_json("answers.expansion.json"))
        ids = [b for b, _ in order]
        self.assertEqual(ids.index("expansion_detail"), ids.index("approach") + 1)

    def test_variant_swaps_content_without_moving_the_block(self):
        base, _ = self.rules.select(_json("answers.baseline.json"))
        exp, _ = self.rules.select(_json("answers.expansion.json"))
        self.assertEqual(dict(base)["scope"], "scope")
        self.assertEqual(dict(exp)["scope"], "scope_expansion")
        self.assertEqual([b for b, _ in base].index("scope") + 1,   # +1: one block added above
                         [b for b, _ in exp].index("scope"))

    def test_blocks_sharing_an_anchor_keep_their_order(self):
        """Inserting each one directly after the anchor reverses them: the
        second placed lands before the first. On the real template that put one
        slide out of place in all 70 decks."""
        rules = Rules({"baseline": ["a", "x", "z"],
                       "blocks": {
                           "first": {"slides": ["first"], "when": "always",
                                     "insert_after": "x"},
                           "second": {"slides": ["second"], "when": "always",
                                      "insert_after": "x"},
                           "third": {"slides": ["third"], "when": "always",
                                     "insert_after": "x"}}})
        self.assertEqual([b for b, _ in rules.select({})[0]],
                         ["a", "x", "first", "second", "third", "z"])

    def test_declaration_order_decides_siblings_not_the_alphabet(self):
        """The merge writes blocks in the decks' own order, so that is the
        order to honour - sorting by id would scramble it."""
        rules = Rules({"baseline": ["x"],
                       "blocks": {
                           "zulu": {"slides": ["zulu"], "when": "always",
                                    "insert_after": "x"},
                           "alpha": {"slides": ["alpha"], "when": "always",
                                     "insert_after": "x"}}})
        self.assertEqual([b for b, _ in rules.select({})[0]],
                         ["x", "zulu", "alpha"])

    def test_a_chain_of_anchors_still_works(self):
        rules = Rules({"baseline": ["x"],
                       "blocks": {
                           "b2": {"slides": ["b2"], "when": "always",
                                  "insert_after": "b1"},
                           "b1": {"slides": ["b1"], "when": "always",
                                  "insert_after": "x"}}})
        self.assertEqual([b for b, _ in rules.select({})[0]], ["x", "b1", "b2"])

    def test_selection_is_deterministic(self):
        answers = _json("answers.expansion.json")
        runs = {tuple(self.rules.select(answers)[0]) for _ in range(5)}
        self.assertEqual(len(runs), 1)

    def test_rules_are_checked_against_the_library(self):
        bad = Rules({"baseline": ["cover", "ghost"],
                     "blocks": {"ghost": {"slides": ["ghost"], "when": "always"}}})
        with Library(LIBRARY) as lib:
            self.assertTrue(any("ghost" in p for p in bad.check_against(lib)))


# ---------------------------------------------------------------- placeholders
class TestPlaceholders(EngineTestCase):
    def test_runs_are_stitched_before_substitution(self):
        para = ('<a:p><a:r><a:t>Local office: </a:t></a:r><a:r><a:t>{{Ci</a:t></a:r>'
                '<a:r><a:t>ty</a:t></a:r><a:r><a:t>}}</a:t></a:r></a:p>')
        out = placeholders.apply(para, {"City": "Leeds"})
        self.assertIn("Leeds", "".join(A_T.findall(out)))
        self.assertNotIn("{{", out)
        self.assertEqual(out.count("<a:r>"), 4, "runs are edited, never removed")

    def test_two_split_placeholders_in_one_paragraph(self):
        """Offsets shift as each placeholder is stitched — hence right-to-left."""
        para = ('<a:p><a:r><a:t>{{A</a:t></a:r><a:r><a:t>aa}} and {{B</a:t></a:r>'
                '<a:r><a:t>bb}}</a:t></a:r></a:p>')
        out = placeholders.apply(para, {"Aaa": "first", "Bbb": "second"})
        self.assertEqual("".join(A_T.findall(out)), "first and second")

    def test_table_markup_is_never_treated_as_text(self):
        """v1's worst bug: a loose <a:t[^>]*> also matched <a:tbl>/<a:tc>/<a:tr>
        and escaped whole tables into garbage (v1-learnings §2)."""
        tbl = ('<a:tbl><a:tblPr firstRow="1"/><a:tr h="10"><a:tc><a:txBody>'
               '<a:p><a:r><a:t>{{ClientName}}</a:t></a:r></a:p></a:txBody></a:tc>'
               '</a:tr></a:tbl>')
        out = placeholders.apply(tbl, {"ClientName": "Acme"})
        self.assertIn("<a:tbl>", out)
        self.assertIn("<a:tc>", out)
        self.assertNotIn("&lt;a:tbl", out)
        self.assertIn("<a:t>Acme</a:t>", out)

    def test_unknown_placeholder_is_left_visible(self):
        para = '<a:p><a:r><a:t>Hello {{Nobody}}</a:t></a:r></a:p>'
        self.assertIn("{{Nobody}}", placeholders.apply(para, {"Someone": "x"}))

    def test_values_containing_markup_are_escaped(self):
        para = '<a:p><a:r><a:t>{{Name}}</a:t></a:r></a:p>'
        out = placeholders.apply(para, {"Name": 'Smith & <Sons>'})
        self.assertIn("Smith &amp; &lt;Sons&gt;", out)
        xml.parsers.expat.ParserCreate().Parse("<r>%s</r>" % out, True)

    def test_marker_removal_takes_the_whole_shape(self):
        sp = ('<p:spTree><p:sp><p:nvSpPr><p:cNvPr id="9" name="keep"/></p:nvSpPr>'
              '<p:txBody><a:p><a:r><a:t>real content</a:t></a:r></a:p></p:txBody></p:sp>'
              '<p:sp><p:nvSpPr><p:cNvPr id="10" name="block-marker"/></p:nvSpPr>'
              '<p:txBody><a:p><a:r><a:t>{{block:cover}}</a:t></a:r></a:p></p:txBody>'
              '</p:sp></p:spTree>')
        out = placeholders.strip_marker(sp)
        self.assertNotIn("{{block:", out)
        self.assertIn("real content", out)
        self.assertEqual(out.count("<p:sp>"), 1)
        self.assertEqual(out.count("</p:sp>"), 1)

    def test_marker_inside_a_group_removes_only_its_own_shape(self):
        grouped = ('<p:grpSp><p:sp><p:txBody><a:p><a:r><a:t>{{block:x}}</a:t>'
                   '</a:r></a:p></p:txBody></p:sp><p:sp><p:txBody><a:p><a:r>'
                   '<a:t>sibling</a:t></a:r></a:p></p:txBody></p:sp></p:grpSp>')
        out = placeholders.strip_marker(grouped)
        self.assertIn("sibling", out)
        self.assertIn("</p:grpSp>", out)
        self.assertEqual(out.count("<p:sp>"), 1)


# ---------------------------------------------------------------- bindings
class TestBindings(EngineTestCase):
    def test_field_data_and_literal(self):
        spec = {"{{A}}": {"from": "field", "field": "Client.Name"},
                "{{B}}": {"from": "data", "source": "profile", "key": "lead_partner"},
                "{{C}}": {"from": "literal", "value": "Demo LLP"}}
        vals, missing = bindings.resolve(spec, {"Client": {"Name": "Acme"}},
                                         {"profile": {"lead_partner": "Dana"}})
        self.assertEqual(vals, {"A": "Acme", "B": "Dana", "C": "Demo LLP"})
        self.assertEqual(missing, [])

    def test_date_formats(self):
        vals, _ = bindings.resolve(
            {"{{D}}": {"from": "field", "field": "DueDate", "format": "long_comma"}},
            {"DueDate": "20261130"})
        self.assertEqual(vals["D"], "November 30, 2026")

    def test_unwired_data_source_is_named_not_guessed(self):
        vals, missing = bindings.resolve(
            {"{{P}}": {"from": "data", "source": "finance", "key": "total"}}, {}, {})
        self.assertEqual(vals, {})
        self.assertEqual(len(missing), 1)
        self.assertIn("finance", missing[0])


# ---------------------------------------------------------------- assembly
class TestAssembly(EngineTestCase):
    def test_baseline_deck(self):
        r = self.build("answers.baseline.json", "baseline.pptx")
        self.assertEqual(r["slides"], 7)
        self.assertEqual(r["unfilled_placeholders"], [])
        self.assertEqual(r["unresolved_bindings"], [])

    def test_expansion_deck_differs_correctly(self):
        r = self.build("answers.expansion.json", "expansion.pptx")
        self.assertEqual(r["slides"], 8)
        blocks = [s["block"] for s in r["order"]]
        self.assertIn("expansion_detail", blocks)
        text = _deck_text(r["out"])
        self.assertIn("Scope of work — expanded", text)
        self.assertNotIn("Review of the interim financial information", text)

    def test_both_decks_validate(self):
        """Decision D5: a build that does not pass never reaches a user."""
        v = _validator()
        if v is None:
            self.skipTest("validate_pptx.py not available")
        for answers, name in (("answers.baseline.json", "v1.pptx"),
                              ("answers.expansion.json", "v2.pptx")):
            r = self.build(answers, name)
            self.assertEqual(v.validate(r["out"]), [], "%s must validate" % name)

    def test_every_part_of_the_output_is_well_formed(self):
        """The v1 validator once passed malformed decks because it never parsed
        the XML. Parse every part, always."""
        r = self.build("answers.expansion.json", "wf.pptx")
        zf = zipfile.ZipFile(r["out"])
        for name in zf.namelist():
            if name.endswith((".xml", ".rels")):
                xml.parsers.expat.ParserCreate().Parse(zf.read(name), True)

    def test_markers_never_ship(self):
        r = self.build("answers.baseline.json", "markers.pptx")
        zf = zipfile.ZipFile(r["out"])
        for n in _slide_parts(zf):
            self.assertNotIn("{{block:", zf.read(n).decode("utf-8"))

    def test_relationship_ids_are_unique_per_part(self):
        r = self.build("answers.expansion.json", "rels.pptx")
        zf = zipfile.ZipFile(r["out"])
        for name in zf.namelist():
            if not name.endswith(".rels"):
                continue
            ids = re.findall(r'Id="([^"]+)"', zf.read(name).decode("utf-8"))
            self.assertEqual(len(ids), len(set(ids)), "duplicate rIds in %s" % name)

    def test_slide_ids_are_unique_and_in_order(self):
        r = self.build("answers.expansion.json", "sldids.pptx")
        prs = zipfile.ZipFile(r["out"]).read("ppt/presentation.xml").decode("utf-8")
        ids = re.findall(r'<p:sldId id="(\d+)"', prs)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), r["slides"])

    def test_every_part_has_a_content_type(self):
        r = self.build("answers.baseline.json", "ct.pptx")
        zf = zipfile.ZipFile(r["out"])
        ct = zf.read("[Content_Types].xml").decode("utf-8")
        defaults = set(re.findall(r'<Default\b[^>]*Extension="([^"]+)"', ct))
        for name in zf.namelist():
            if name == "[Content_Types].xml":
                continue
            # rsplit, not splitext: '_rels/.rels' has no stem, so splitext
            # reports no extension for it.
            base = os.path.basename(name)
            ext = base.rsplit(".", 1)[1].lower() if "." in base else ""
            has = ('PartName="/%s"' % name) in ct or ext in defaults
            self.assertTrue(has, "%s has no content type" % name)

    def test_no_dangling_or_stale_slide_declarations(self):
        """Content-type Overrides for library slides we did not emit would point
        at parts that are not in the package."""
        r = self.build("answers.baseline.json", "stale.pptx")
        zf = zipfile.ZipFile(r["out"])
        ct = zf.read("[Content_Types].xml").decode("utf-8")
        declared = set(re.findall(r'<Override\b[^>]*PartName="/(ppt/slides/[^"]+)"', ct))
        self.assertEqual(declared, set(_slide_parts(zf)))

    def test_a_block_may_appear_twice(self):
        """Repeating a block must emit two distinct parts, not one shared one."""
        rules = Rules({"baseline": ["cover", "about_us", "cover"],
                       "placeholders": {"{{ClientName}}": {"from": "literal",
                                                           "value": "Acme"}}})
        r = build(LIBRARY, rules, {}, self.out("twice.pptx"), data_sources={})
        self.assertEqual(r["slides"], 3)
        zf = zipfile.ZipFile(r["out"])
        self.assertEqual(len(_slide_parts(zf)), 3)
        v = _validator()
        if v:
            self.assertEqual(v.validate(r["out"]), [])

    def test_strict_refuses_to_ship_a_gap(self):
        with self.assertRaises(AssemblyError):
            build(LIBRARY, self.rules, _json("answers.baseline.json"),
                  self.out("strict.pptx"), data_sources={}, strict=True)
        self.assertFalse(os.path.exists(self.out("strict.pptx")),
                         "a failed strict build must not leave a deck behind")

    def test_unwired_bindings_are_reported_but_build_succeeds(self):
        r = build(LIBRARY, self.rules, _json("answers.baseline.json"),
                  self.out("loose.pptx"), data_sources={})
        self.assertTrue(r["unresolved_bindings"])
        self.assertIn("LeadPartner", " ".join(r["unfilled_placeholders"]))
        self.assertTrue(os.path.exists(r["out"]))

    def test_empty_selection_is_refused(self):
        rules = Rules({"baseline": ["cover"],
                       "blocks": {"cover": {"slides": ["cover"], "when": "never"}}})
        with self.assertRaises(AssemblyError):
            build(LIBRARY, rules, {}, self.out("empty.pptx"))

    def test_the_same_inputs_produce_the_same_deck(self):
        """Determinism is the whole promise: no AI, no clock, no randomness."""
        a = self.build("answers.expansion.json", "det1.pptx")
        b = self.build("answers.expansion.json", "det2.pptx")
        za, zb = zipfile.ZipFile(a["out"]), zipfile.ZipFile(b["out"])
        self.assertEqual(sorted(za.namelist()), sorted(zb.namelist()))
        for n in za.namelist():
            self.assertEqual(za.read(n), zb.read(n), "%s differs between builds" % n)


# ---------------------------------------------------------------- templates
class TestTemplates(EngineTestCase):
    """Adding a template must be a file operation, never a code change.

    The fixture is the demo deck with every `{{block:id}}` marker removed —
    a stand-in for a firm's existing template that nobody has annotated.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = os.path.join(HERE, "fixtures", "office_template.pptx")
        if not os.path.exists(cls.fixture):
            os.makedirs(os.path.dirname(cls.fixture), exist_ok=True)
            sys.path.insert(0, os.path.join(ROOT, "tools"))
            import make_demo_library as mk
            was, mk.MARKERS = mk.MARKERS, False
            try:
                mk.write_library(cls.fixture)
            finally:
                mk.MARKERS = was

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pptgen2_tpl_", dir=self.tmp)

    def imported(self, name="office", **kw):
        return import_deck(self.fixture, self.root, name=name, **kw)

    # -- identity ---------------------------------------------------------
    def test_unmarked_deck_gets_ids_from_slide_titles(self):
        r = self.imported()
        self.assertEqual(r["ids_from_markers"], 0)
        self.assertEqual(r["ids_from_position"], 0)
        self.assertEqual(r["ids_from_titles"], r["slides"])
        ids = [b["id"] for b in r["blocks"]]
        self.assertIn("about_us", ids)
        self.assertIn("proposed_fees", ids)

    def test_a_marker_beats_the_sidecar(self):
        """The deck is the source of truth when it says something."""
        with Library(LIBRARY, block_map={"slide1.xml": "from_sidecar"}) as lib:
            self.assertIn("cover", lib.blocks)
            self.assertNotIn("from_sidecar", lib.blocks)
            self.assertEqual(lib.block("cover").source, "marker")

    def test_the_sidecar_beats_a_guessed_title(self):
        with Library(self.fixture, block_map={"slide2.xml": "the_firm"}) as lib:
            self.assertIn("the_firm", lib.blocks)
            self.assertEqual(lib.block("the_firm").source, "map")
            self.assertNotIn("about_us", lib.blocks)

    def test_sidecar_ids_survive_a_deck_reorder(self):
        """Positional ids break on reorder; that is the whole reason the sidecar
        exists. Mapped ids are keyed to the slide part, not its position."""
        with Library(self.fixture) as lib:
            by_part = {os.path.basename(b.part): b.id for b in lib.ordered()}
        mapping = {"slide9.xml": "contacts", "slide1.xml": "cover"}
        with Library(self.fixture, block_map=mapping) as lib:
            self.assertEqual(lib.block("contacts").part, "ppt/slides/slide9.xml")
            self.assertEqual(lib.block("cover").part, "ppt/slides/slide1.xml")
        self.assertNotEqual(by_part["slide9.xml"], "contacts",
                            "the guessed id differs — so the map really was used")

    def test_two_slides_with_the_same_title_do_not_collide(self):
        r = self.imported()
        ids = [b["id"] for b in r["blocks"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_ids_in_the_sidecar_are_refused(self):
        """A guessed clash is disambiguated; a declared clash is an error."""
        with self.assertRaises(LibraryError):
            Library(self.fixture, block_map={"slide1.xml": "same",
                                             "slide2.xml": "same"})

    def test_strict_ids_refuses_a_deck_nothing_names(self):
        with self.assertRaises(LibraryError):
            Library(self.fixture, block_map={}, strict_ids=True)

    # -- the import contract ----------------------------------------------
    def test_import_writes_a_template_that_builds_with_no_edits(self):
        r = self.imported()
        for f in ("template.json", "library.pptx", "blocks.json", "rules.json"):
            self.assertTrue(os.path.exists(os.path.join(r["folder"], f)), f)
        tpl = Template(r["folder"])
        report = build_template(tpl, {}, self.out("imported.pptx"))
        self.assertEqual(report["slides"], r["slides"],
                         "starter rules = every slide, in the deck's own order")
        v = _validator()
        if v:
            self.assertEqual(v.validate(report["out"]), [])

    def test_import_copies_the_deck_byte_for_byte(self):
        """We never modify the source template — that is the promise that makes
        importing a firm deck safe."""
        r = self.imported()
        self.assertEqual(open(self.fixture, "rb").read(),
                         open(r["library"], "rb").read())

    def test_import_discovers_and_pre_binds_placeholders(self):
        r = self.imported()
        rules = json.load(open(os.path.join(r["folder"], "rules.json"),
                               encoding="utf-8"))
        self.assertIn("{{ClientName}}", rules["placeholders"])
        self.assertEqual(rules["placeholders"]["{{ClientName}}"],
                         {"from": "field", "field": "ClientName"})

    def test_import_refuses_a_non_pptx(self):
        junk = os.path.join(self.root, "notes.txt")
        open(junk, "w").write("not a deck")
        with self.assertRaises(TemplateError):
            import_deck(junk, self.root, name="junk")

    def test_import_will_not_silently_replace_a_template(self):
        self.imported()
        with self.assertRaises(TemplateError):
            self.imported()
        self.imported(overwrite=True)          # explicit is fine

    def test_a_failed_import_leaves_nothing_behind(self):
        bad = os.path.join(self.root, "empty.pptx")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("hello.txt", "not a deck")
        with self.assertRaises(LibraryError):
            import_deck(bad, self.root, name="broken")
        self.assertFalse(os.path.exists(os.path.join(self.root, "broken")))

    # -- maintenance ------------------------------------------------------
    # -- reconciling after the deck is rewritten --------------------------
    def _renumber(self, tpl, rename):
        """Rewrite the sidecar's keys, as if the parts had been renumbered."""
        raw = tpl.raw_block_map
        tpl.write_block_map({rename(part): entry for part, entry in raw.items()})

    def test_reconcile_does_nothing_when_the_deck_has_not_moved(self):
        tpl = Template(self.imported()["folder"])
        before = open(tpl.blocks_path, encoding="utf-8").read()
        report = tpl.reconcile_blocks()
        self.assertFalse(report["changed"])
        self.assertEqual(report["unmatched"], [])
        self.assertEqual(report["vanished"], [])
        self.assertEqual(open(tpl.blocks_path, encoding="utf-8").read(), before,
                         "a no-op must not rewrite the file")

    def test_reconcile_rekeys_after_a_repair_renumbers_the_slides(self):
        """PowerPoint rewrites part names when it repairs a deck. The sidecar
        is keyed by those names, so every id would end up on the wrong slide -
        silently, since nothing about it is invalid."""
        folder = self.imported()["folder"]
        tpl = Template(folder)
        expected = dict(tpl.block_map)               # part -> id, before
        self._renumber(tpl, lambda p: p.replace("slide", "old"))

        report = Template(folder).reconcile_blocks()
        self.assertTrue(report["changed"])
        self.assertEqual(len(report["moved"]), len(expected))
        self.assertEqual(report["unmatched"], [])
        self.assertEqual(report["vanished"], [])
        self.assertEqual(Template(folder).block_map, expected,
                         "every id must land back on the slide it named")

    def test_reconciled_ids_actually_resolve_in_the_library(self):
        folder = self.imported()["folder"]
        tpl = Template(folder)
        self._renumber(tpl, lambda p: p.replace("slide", "old"))
        Template(folder).reconcile_blocks()
        with Template(folder).open_library() as lib:
            self.assertTrue(all(b.source == "map" for b in lib.ordered()))

    def test_reconcile_reports_what_it_cannot_place(self):
        """Better to say a block is lost than to guess a slide for it."""
        folder = self.imported()["folder"]
        tpl = Template(folder)
        raw = tpl.raw_block_map
        part = sorted(raw)[0]
        raw["gone.xml"] = {"id": "a_deleted_block",
                           "title": "A slide that is no longer here"}
        raw.pop(part)
        tpl.write_block_map(raw)

        report = Template(folder).reconcile_blocks()
        lost = [bid for bid, _was in report["unmatched"]]
        self.assertIn("a_deleted_block", lost)
        self.assertIn(part, report["vanished"],
                      "a slide nothing points at should be reported too")
        # The entry stays in the file rather than being deleted — a retitled
        # slide also lands here, and silently dropping a curated id would be
        # worse than leaving one that points nowhere. What matters is that it
        # is inert: it must never attach itself to some other slide.
        with Template(folder).open_library() as lib:
            self.assertNotIn("a_deleted_block", lib.blocks,
                             "an unplaceable id must not land on a real slide")

    def test_reconcile_survives_a_template_with_no_sidecar(self):
        """The demo names its blocks with markers, so there is nothing to key."""
        report = Template(DEMO).reconcile_blocks()
        self.assertFalse(report["changed"])

    def test_rename_updates_the_sidecar_and_the_rules_together(self):
        r = self.imported()
        tpl = Template(r["folder"])
        old = [b["id"] for b in r["blocks"]][0]
        tpl.rename_block(old, "cover")
        self.assertIn("cover", Template(r["folder"]).block_map.values())
        rules = json.load(open(tpl.rules_path, encoding="utf-8"))
        self.assertIn("cover", rules["baseline"])
        self.assertNotIn(old, rules["baseline"])
        with Template(r["folder"]).open_library() as lib:
            self.assertEqual(lib.block("cover").source, "map")

    def test_rename_refuses_a_taken_id(self):
        tpl = Template(self.imported()["folder"])
        ids = list(tpl.block_map.values())
        with self.assertRaises(TemplateError):
            tpl.rename_block(ids[0], ids[1])

    def test_drift_is_reported_when_a_slide_is_retitled(self):
        r = self.imported()
        tpl = Template(r["folder"])
        raw = tpl.raw_block_map
        part = sorted(raw)[0]
        raw[part] = dict(raw[part], title="Something else entirely")
        tpl.write_block_map(raw)
        with Template(r["folder"]).open_library() as lib:
            drift = lib.map_drift()
        self.assertTrue(any("title was" in d for d in drift))

    def test_listing_finds_imported_templates(self):
        self.imported(name="alpha")
        self.imported(name="beta")
        self.assertEqual([t.name for t in list_templates(self.root)],
                         ["alpha", "beta"])

    def test_find_template_by_name_or_path(self):
        r = self.imported(name="gamma")
        self.assertEqual(find_template(self.root, "gamma").name, "gamma")
        self.assertEqual(find_template(self.root, r["folder"]).name, "gamma")
        with self.assertRaises(TemplateError):
            find_template(self.root, "nope")

    def test_the_shipped_demo_template_still_loads(self):
        tpl = Template(DEMO)
        self.assertEqual(tpl.name, "demo")
        with tpl.open_library() as lib:
            self.assertEqual(len(lib.blocks), 9)
            self.assertTrue(all(b.source == "marker" for b in lib.blocks.values()))
        report = build_template(tpl, _json("answers.expansion.json"),
                                self.out("via_template.pptx"))
        self.assertEqual(report["slides"], 8)


# ---------------------------------------------------------------- preview
class TestSvgPreview(EngineTestCase):
    """The renderer is an approximation, so these tests check the things that
    are objectively true — well-formed output, real geometry, nothing invented,
    and no crash on anything the library can throw at it. Whether it *looks*
    right is judged from the contact sheet (`build.py preview`), which is what
    that command exists for."""

    def render(self, block_id, **kw):
        with Library(LIBRARY) as lib:
            return svgmod.render_block(lib, block_id, **kw)

    def test_every_block_renders_to_well_formed_svg(self):
        results = svgmod.render_template(Template(DEMO))
        self.assertEqual(len(results), 9)
        for r in results:
            self.assertIsNone(r.get("error"), "%s: %s" % (r["id"], r.get("error")))
            xml.parsers.expat.ParserCreate().Parse(r["svg"].encode("utf-8"), True)
            self.assertIn('viewBox="0 0 12192000 6858000"', r["svg"])

    def test_slide_size_comes_from_the_deck(self):
        r = self.render("cover")
        self.assertEqual((r["width"], r["height"]), (12192000, 6858000))

    def test_text_is_rendered_not_dropped(self):
        r = self.render("about_us")
        text = "".join(re.findall(r'<tspan[^>]*>(.*?)</tspan>', r["svg"]))
        self.assertIn("About us", text)
        self.assertIn("national audit practice", text,
                      "words must not be split across whitespace-only tspans")

    def test_placeholders_are_tagged_for_highlighting(self):
        """The UI lights up where values land; the tag has to be in the SVG."""
        r = self.render("cover")
        lit = re.findall(r'class="ph"[^>]*>([^<]*)<', r["svg"])
        self.assertIn("{{ClientName}}", lit)
        self.assertIn("{{DueDate}}", lit)

    def test_a_split_placeholder_highlights_as_one(self):
        """`{{City}}` is authored across four runs. Without the same stitching
        the build path uses, the preview would light up `{{Ci`, `ty`, `}}`."""
        r = self.render("contacts")
        self.assertIn("{{City}}", re.findall(r'class="ph"[^>]*>([^<]*)<', r["svg"]))

    def test_block_markers_never_appear_in_a_preview(self):
        for r in svgmod.render_template(Template(DEMO)):
            self.assertNotIn("{{block:", r["svg"])

    def test_tables_are_drawn_as_real_cells(self):
        r = self.render("fees")
        text = " ".join(re.findall(r'<tspan[^>]*>(.*?)</tspan>', r["svg"]))
        for cell in ("Phase", "Timing", "Fee", "Q1"):
            self.assertIn(cell, text)
        self.assertGreaterEqual(r["svg"].count("<rect"), 15, "one rect per cell")

    def test_theme_colours_resolve_through_the_colour_map(self):
        """`schemeClr val="bg1"` goes through the master's clrMap before the
        theme. Getting that wrong swaps foreground and background."""
        theme = svgmod.Theme(None, {"bg1": "lt1", "tx1": "dk1"})
        self.assertEqual(theme.resolve("bg1"), "FFFFFF")
        self.assertEqual(theme.resolve("tx1"), "000000")

    def test_colour_modifiers_are_applied(self):
        el = ET.fromstring(
            '<solidFill xmlns="%s"><srgbClr val="FF0000"><alpha val="50000"/>'
            '</srgbClr></solidFill>' % svgmod.A)
        colour, alpha = svgmod._colour_from(el, svgmod.Theme())
        self.assertEqual(colour, "#FF0000")
        self.assertAlmostEqual(alpha, 0.5, places=3)

    def test_markup_in_slide_text_is_escaped(self):
        """A slide reading '<b> & co' must not produce broken SVG."""
        r = self.render("fees")
        self.assertIn("&amp;", r["svg"])
        xml.parsers.expat.ParserCreate().Parse(r["svg"].encode("utf-8"), True)

    def test_an_unrenderable_slide_does_not_break_the_set(self):
        """render_template must return something for every block, always —
        the console shows a broken slide, it does not fail to load."""
        original = svgmod.render_slide
        try:
            def boom(lib, part, **kw):
                if part.endswith("slide3.xml"):
                    raise ValueError("simulated failure")
                return original(lib, part, **kw)
            svgmod.render_slide = boom
            results = svgmod.render_template(Template(DEMO))
        finally:
            svgmod.render_slide = original
        self.assertEqual(len(results), 9)
        broken = [r for r in results if r.get("error")]
        self.assertEqual(len(broken), 1)
        self.assertIsNone(broken[0]["svg"])

    def test_rendering_is_fast_enough_to_be_synchronous(self):
        """The whole reason for rendering in-process: import stays a normal
        request instead of a background job."""
        start = time.time()
        svgmod.render_template(Template(DEMO))
        elapsed = time.time() - start
        self.assertLess(elapsed, 3.0, "9 slides took %.2fs" % elapsed)

    def test_output_is_small(self):
        results = svgmod.render_template(Template(DEMO))
        total = sum(len(r["svg"]) for r in results)
        self.assertLess(total, 400_000, "%d bytes for 9 slides" % total)

    def test_the_text_card_fallback_reports_what_is_there(self):
        with Library(LIBRARY) as lib:
            card = svgmod.block_card(lib, "fees")
        self.assertEqual(card["tables"], 1)
        self.assertIn("Proposed fees", card["text"])
        self.assertIn("FeeTotal", card["placeholders"])
        self.assertNotIn("{{block:fees}}", " ".join(card["text"]))

    def test_an_unmarked_deck_renders_too(self):
        """Imported firm templates have no markers; preview must still work."""
        fixture = os.path.join(HERE, "fixtures", "office_template.pptx")
        if not os.path.exists(fixture):
            os.makedirs(os.path.dirname(fixture), exist_ok=True)
            sys.path.insert(0, os.path.join(ROOT, "tools"))
            import make_demo_library as mk
            was, mk.MARKERS = mk.MARKERS, False
            try:
                mk.write_library(fixture)
            finally:
                mk.MARKERS = was
        with Library(fixture) as lib:
            r = svgmod.render_block(lib, "about_us")
        xml.parsers.expat.ParserCreate().Parse(r["svg"].encode("utf-8"), True)
        self.assertIn("About us", "".join(
            re.findall(r'<tspan[^>]*>(.*?)</tspan>', r["svg"])))


# ---------------------------------------------------------------- merging
class TestMasterMerge(EngineTestCase):
    """Merging the generated Templafy decks back into one library.

    Synthesised here the same way the real ones were made — OFAT payloads that
    differ in exactly one answer — so the merge, the rule proposal and the
    round trip are all exercised without needing a confidential deck.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import build_master_deck
        cls.bmd = build_master_deck

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pptgen2_merge_", dir=self.tmp)
        self.decks = os.path.join(self.work, "decks")
        self.pays = os.path.join(self.work, "payloads")
        self.roots = os.path.join(self.work, "templates")
        for d in (self.decks, self.pays, self.roots):
            os.makedirs(d)

        common = {"FullClientName": "Example Corporation",
                  "ShortClientName": "Example", "DueDate": "20261130",
                  "City": "New York"}
        self.cases = {"00_baseline": dict(common, AuditType="Statutory Audit"),
                      "01_expansion": dict(common,
                                           AuditType="Expansion of Services")}
        for name, payload in self.cases.items():
            with open(os.path.join(self.pays, name + ".json"), "w") as fh:
                json.dump(payload, fh)
            build(LIBRARY, self.rules, payload,
                  os.path.join(self.decks, name + ".pptx"), data_sources=self.data)

    def merge(self, *extra):
        argv = [self.decks, "--payloads", self.pays, "--templates", self.roots,
                "--name", "merged", "--overwrite"] + list(extra)
        buffer = io.StringIO()                    # the tool reports to a human
        with contextlib.redirect_stdout(buffer):
            code = self.bmd.main(argv)
        self.report = buffer.getvalue()
        return code

    def merged(self):
        return Template(os.path.join(self.roots, "merged"))

    def rules_json(self):
        with open(os.path.join(self.roots, "merged", "rules.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    # -- the merge --------------------------------------------------------
    def test_decks_collapse_to_the_union_of_their_slides(self):
        """7 + 8 slide instances, 9 distinct slides."""
        self.assertEqual(self.merge(), 0)
        with self.merged().open_library() as lib:
            self.assertEqual(len(lib.blocks), 9)

    def test_the_master_validates(self):
        """It is assembled from parts of several packages — the one thing that
        must never break."""
        self.merge()
        v = _validator()
        if v is None:
            self.skipTest("validate_pptx.py not available")
        self.assertEqual(
            v.validate(os.path.join(self.roots, "merged", "library.pptx")), [])

    def test_shared_layouts_and_media_are_stored_once(self):
        """Both decks carry the same master, layout and theme. Copying them per
        deck would give a library with 70 of each."""
        self.merge()
        z = zipfile.ZipFile(os.path.join(self.roots, "merged", "library.pptx"))
        layouts = [n for n in z.namelist() if "slideLayouts/slideLayout" in n
                   and n.endswith(".xml")]
        masters = [n for n in z.namelist() if "slideMasters/slideMaster" in n
                   and n.endswith(".xml")]
        self.assertEqual(len(layouts), 1)
        self.assertEqual(len(masters), 1)

    def test_slides_are_copied_byte_for_byte(self):
        """The merge must never redraw a slide — only move it."""
        self.merge()
        src = zipfile.ZipFile(os.path.join(self.decks, "00_baseline.pptx"))
        out = zipfile.ZipFile(os.path.join(self.roots, "merged", "library.pptx"))
        src_text = {" ".join(A_T.findall(src.read(n).decode("utf-8")))
                    for n in _slide_parts(src)}
        out_text = {" ".join(A_T.findall(out.read(n).decode("utf-8")))
                    for n in _slide_parts(out)}
        self.assertTrue(src_text <= out_text,
                        "every slide of the source deck survives the merge")

    # -- the shape of a real Templafy deck --------------------------------
    def _templafy_ify(self, src, dst, rid="Rf2b6b82092c644ea"):
        """Make a deck look like Templafy's output in the two ways that broke us.

        A GUID-style relationship id on a presentation-level attachment that is
        neither a slide nor a master (an embedded font), and a binary part
        hanging off a slide (what a chart's workbook is). Neither exists in the
        synthetic library, which is why 100 tests passed while PowerPoint
        refused to open the real merge.
        """
        z = zipfile.ZipFile(src)
        rels = z.read("ppt/_rels/presentation.xml.rels").decode()
        prs = z.read("ppt/presentation.xml").decode()
        rels = rels.replace("</Relationships>",
                            '<Relationship Id="%s" Type="http://schemas.'
                            'openxmlformats.org/officeDocument/2006/'
                            'relationships/font" Target="fonts/font1.fntdata"/>'
                            "</Relationships>" % rid)
        prs = prs.replace("<p:sldSz",
                          '<p:embeddedFontLst><p:embeddedFont>'
                          '<p:font typeface="Arial"/><p:regular r:id="%s"/>'
                          '</p:embeddedFont></p:embeddedFontLst><p:sldSz' % rid)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                name = info.filename
                if name == "ppt/_rels/presentation.xml.rels":
                    out.writestr(name, rels)
                elif name == "ppt/presentation.xml":
                    out.writestr(name, prs)
                elif name == "[Content_Types].xml":
                    out.writestr(name, z.read(name).decode().replace(
                        "</Types>", '<Default Extension="fntdata" '
                        'ContentType="application/x-fontdata"/>'
                        '<Default Extension="xlsx" ContentType="application/'
                        'vnd.openxmlformats-officedocument.spreadsheetml.sheet"'
                        '/></Types>'))
                elif name == "ppt/slides/_rels/slide1.xml.rels":
                    out.writestr(name, z.read(name).decode().replace(
                        "</Relationships>",
                        '<Relationship Id="rIdOle" Type="http://schemas.'
                        'openxmlformats.org/officeDocument/2006/relationships/'
                        'oleObject" Target="../embeddings/book.xlsx"/>'
                        "</Relationships>"))
                elif name == "ppt/slides/slide1.xml":
                    # the slide must actually reference it - an unreferenced
                    # rel is dropped, and rightly so
                    out.writestr(name, z.read(name).decode().replace(
                        "</p:spTree>",
                        '<p:graphicFrame><p:nvGraphicFramePr>'
                        '<p:cNvPr id="99" name="Embedded book"/>'
                        '<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
                        '<p:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/>'
                        '</p:xfrm><a:graphic><a:graphicData uri="http://schemas.'
                        'openxmlformats.org/presentationml/2006/ole">'
                        '<p:oleObj spid="_x0000_s1026" r:id="rIdOle" imgW="100" '
                        'imgH="100" progId="Excel.Sheet.12"><p:embed/></p:oleObj>'
                        '</a:graphicData></a:graphic></p:graphicFrame>'
                        "</p:spTree>"))
                else:
                    out.writestr(info, z.read(name))
            out.writestr("ppt/fonts/font1.fntdata", b"\x00FONTDATA")
            out.writestr("ppt/embeddings/book.xlsx", b"PK\x03\x04binary-not-xml")
        z.close()

    def _templafy_decks(self):
        folder = os.path.join(self.work, "tdecks")
        os.makedirs(folder, exist_ok=True)
        for name in self.cases:
            self._templafy_ify(os.path.join(self.decks, name + ".pptx"),
                               os.path.join(folder, name + ".pptx"))
        return folder

    def test_relationship_targets_are_relative(self):
        """Package-absolute targets ("/ppt/...") are legal and Templafy emits
        them, but PowerPoint's own files are relative throughout. A package it
        refuses to open is not the place to rely on a tolerance we cannot test
        here - no test in this suite opens PowerPoint."""
        decks = self._templafy_decks()
        with contextlib.redirect_stdout(io.StringIO()):
            self.bmd.main([decks, "--payloads", self.pays, "--templates",
                           self.roots, "--name", "rel", "--overwrite"])
        z = zipfile.ZipFile(os.path.join(self.roots, "rel", "library.pptx"))
        absolute = []
        for name in z.namelist():
            if not name.endswith(".rels"):
                continue
            for tag in ooxmlmod.rel_tags(z.read(name).decode()):
                if 'TargetMode="External"' in tag:
                    continue
                target = ooxmlmod.rel_attr(tag, "Target") or ""
                if target.startswith("/"):
                    absolute.append("%s -> %s" % (name, target))
        self.assertEqual(absolute, [])

    def test_smartart_relationships_are_not_dropped(self):
        """SmartArt points at its four parts with r:dm / r:lo / r:qs / r:cs.
        A keeper that only knows r:id, r:embed and r:link drops all four and
        leaves the slide referencing relationships that no longer exist."""
        rt = ("http://schemas.openxmlformats.org/officeDocument/2006/"
              "relationships/")
        rels = ooxmlmod.build_rels([
            '<Relationship Id="rId1" Type="%sslideLayout" '
            'Target="../slideLayouts/slideLayout1.xml"/>' % rt,
            '<Relationship Id="rId4" Type="%sdiagramData" '
            'Target="../diagrams/data1.xml"/>' % rt,
            '<Relationship Id="rId5" Type="%sdiagramLayout" '
            'Target="../diagrams/layout1.xml"/>' % rt,
            '<Relationship Id="rId6" Type="%sdiagramQuickStyle" '
            'Target="../diagrams/quickStyle1.xml"/>' % rt,
            '<Relationship Id="rId7" Type="%sdiagramColors" '
            'Target="../diagrams/colors1.xml"/>' % rt,
            '<Relationship Id="rId9" Type="%snotesSlide" '
            'Target="../notesSlides/notesSlide1.xml"/>' % rt])
        slide = ('<p:sld><p:cSld><p:spTree><p:graphicFrame><a:graphic>'
                 '<a:graphicData><dgm:relIds r:dm="rId4" r:lo="rId5" '
                 'r:qs="rId6" r:cs="rId7"/></a:graphicData></a:graphic>'
                 '</p:graphicFrame></p:spTree></p:cSld></p:sld>')
        kept = ooxmlmod.keep_referenced_rels(slide, rels)
        for rid in ("rId4", "rId5", "rId6", "rId7"):
            self.assertIn(rid, kept, "SmartArt part %s was dropped" % rid)
        self.assertNotIn("rId9", kept, "unreferenced notes should still go")

    def test_a_templafy_shaped_merge_validates(self):
        """The real 70-deck merge produced a library PowerPoint would not open:
        binary parts renamed to .xml, and eight GUID relationship ids in
        presentation.xml left pointing at nothing."""
        decks = self._templafy_decks()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.bmd.main([decks, "--payloads", self.pays, "--templates",
                                  self.roots, "--name", "tf", "--overwrite"])
        self.assertEqual(code, 0, buffer.getvalue())
        from engine import validate as validator
        library = os.path.join(self.roots, "tf", "library.pptx")
        self.assertEqual(validator.validate(library), [])

    def test_presentation_relationships_all_resolve(self):
        """Every r:id presentation.xml uses must exist in its .rels. This is
        the check that would have caught the deck PowerPoint refused."""
        decks = self._templafy_decks()
        with contextlib.redirect_stdout(io.StringIO()):
            self.bmd.main([decks, "--payloads", self.pays, "--templates",
                           self.roots, "--name", "tf2", "--overwrite"])
        z = zipfile.ZipFile(os.path.join(self.roots, "tf2", "library.pptx"))
        prs = z.read("ppt/presentation.xml").decode()
        rels = z.read("ppt/_rels/presentation.xml.rels").decode()
        used = set(re.findall(r'r:id="([^"]+)"', prs))
        have = set(re.findall(r'Id="([^"]+)"', rels))
        self.assertEqual(used - have, set())
        self.assertIn("Rf2b6b82092c644ea", have, "the GUID id must be kept")
        self.assertIn("ppt/fonts/font1.fntdata", z.namelist(),
                      "its target must be carried too")

    def test_binary_dependencies_keep_their_extension(self):
        """A chart's embedded workbook written as .xml is binary in a part
        declared as XML - PowerPoint stops reading the file."""
        decks = self._templafy_decks()
        with contextlib.redirect_stdout(io.StringIO()):
            self.bmd.main([decks, "--payloads", self.pays, "--templates",
                           self.roots, "--name", "tf3", "--overwrite"])
        z = zipfile.ZipFile(os.path.join(self.roots, "tf3", "library.pptx"))
        for name in z.namelist():
            if name.endswith(".xml"):
                self.assertFalse(z.read(name).startswith(b"PK\x03\x04"),
                                 "%s is a zip archive named .xml" % name)
        self.assertTrue(any(n.endswith(".xlsx") for n in z.namelist()),
                        "the workbook should keep its real extension")

    def _two_master_decks(self):
        """Decks with two masters, like a real branded template. The synthetic
        library has one, which is why multi-master merging went untested."""
        folder = os.path.join(self.work, "mm")
        os.makedirs(folder, exist_ok=True)
        for name in self.cases:
            self._add_second_master(os.path.join(self.decks, name + ".pptx"),
                                    os.path.join(folder, name + ".pptx"))
        return folder

    def _add_second_master(self, src, dst):
        z = zipfile.ZipFile(src)
        rt = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
        base = "application/vnd.openxmlformats-officedocument.presentationml."
        layout2 = z.read("ppt/slideLayouts/slideLayout1.xml").decode().replace(
            'name="Blank"', 'name="Section"')
        master2 = z.read("ppt/slideMasters/slideMaster1.xml").decode().replace(
            "<p:spTree>", "<p:spTree><!--second-->")
        prs = z.read("ppt/presentation.xml").decode().replace(
            "</p:sldMasterIdLst>",
            '<p:sldMasterId id="2147483649" r:id="rIdM2"/></p:sldMasterIdLst>')
        prels = z.read("ppt/_rels/presentation.xml.rels").decode().replace(
            "</Relationships>",
            '<Relationship Id="rIdM2" Type="%sslideMaster" '
            'Target="slideMasters/slideMaster2.xml"/></Relationships>' % rt)
        ct = z.read("[Content_Types].xml").decode().replace(
            "</Types>",
            '<Override PartName="/ppt/slideMasters/slideMaster2.xml" '
            'ContentType="%sslideMaster+xml"/>'
            '<Override PartName="/ppt/slideLayouts/slideLayout2.xml" '
            'ContentType="%sslideLayout+xml"/></Types>' % (base, base))
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                n = info.filename
                if n == "ppt/presentation.xml":
                    out.writestr(n, prs)
                elif n == "ppt/_rels/presentation.xml.rels":
                    out.writestr(n, prels)
                elif n == "[Content_Types].xml":
                    out.writestr(n, ct)
                elif n in ("ppt/slides/_rels/slide2.xml.rels",
                           "ppt/slides/_rels/slide3.xml.rels"):
                    # slides present in BOTH decks, so the layout is actually
                    # reached - retargeting ones that de-duplicate away would
                    # leave the second master unused and prove nothing
                    out.writestr(n, z.read(n).decode().replace(
                        "slideLayout1.xml", "slideLayout2.xml"))
                else:
                    out.writestr(info, z.read(n))
            out.writestr("ppt/slideMasters/slideMaster2.xml", master2)
            out.writestr("ppt/slideMasters/_rels/slideMaster2.xml.rels",
                         ooxmlmod.build_rels([
                             '<Relationship Id="rId1" Type="%sslideLayout" '
                             'Target="../slideLayouts/slideLayout2.xml"/>' % rt,
                             '<Relationship Id="rId2" Type="%stheme" '
                             'Target="../theme/theme1.xml"/>' % rt]))
            out.writestr("ppt/slideLayouts/slideLayout2.xml", layout2)
            out.writestr("ppt/slideLayouts/_rels/slideLayout2.xml.rels",
                         ooxmlmod.build_rels([
                             '<Relationship Id="rId1" Type="%sslideMaster" '
                             'Target="../slideMasters/slideMaster2.xml"/>' % rt]))
        z.close()

    def test_a_multi_master_merge_keeps_both_masters_and_validates(self):
        """A layout that slides use, that names its master, but that the master
        does not list, makes PowerPoint repair the deck and then give up. Every
        individual relationship still resolves, so no link check catches it -
        the validator's master/layout consistency check does."""
        decks = self._two_master_decks()
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.bmd.main([decks, "--templates", self.roots,
                                  "--name", "mm", "--overwrite"])
        self.assertEqual(code, 0)
        library = os.path.join(self.roots, "mm", "library.pptx")
        z = zipfile.ZipFile(library)
        masters = [n for n in z.namelist()
                   if "slideMasters/slideMaster" in n and n.endswith(".xml")]
        layouts = [n for n in z.namelist()
                   if "slideLayouts/slideLayout" in n and n.endswith(".xml")]
        self.assertEqual(len(masters), 2, "both masters should survive")
        self.assertEqual(len(layouts), 2, "both layouts should survive")

        from engine import validate as validator
        self.assertEqual(validator.validate(library), [])

    def test_the_validator_catches_an_orphaned_layout(self):
        """The check that was missing when PowerPoint refused a library this
        tool had just called clean."""
        decks = self._two_master_decks()
        with contextlib.redirect_stdout(io.StringIO()):
            self.bmd.main([decks, "--templates", self.roots, "--name", "orph",
                           "--overwrite"])
        library = os.path.join(self.roots, "orph", "library.pptx")
        damaged = os.path.join(self.work, "orphaned.pptx")
        z = zipfile.ZipFile(library)
        with zipfile.ZipFile(damaged, "w") as out:
            for info in z.infolist():
                body = z.read(info.filename)
                if ("slideMasters/slideMaster" in info.filename
                        and info.filename.endswith(".xml")):
                    # drop one layout from the master's list, leaving the
                    # layout itself present and still pointing back
                    body = re.sub(rb'<p:sldLayoutId [^>]*/>', b'', body)
                out.writestr(info.filename, body)
        z.close()
        from engine import validate as validator
        codes = [i[0] for i in validator.validate(damaged)]
        self.assertIn("orphan-layout", codes)

    # -- rule proposal ----------------------------------------------------
    def test_the_controlling_answer_is_identified(self):
        """The whole point of pairing decks with payloads: work out *why* a
        slide was in the deck."""
        self.merge()
        rules = self.rules_json()
        blocks = rules["blocks"]
        self.assertTrue(blocks, "conditional blocks should be proposed")
        for bid, spec in blocks.items():
            self.assertEqual(spec["when"]["field"], "AuditType", bid)
        by_value = {spec["when"]["eq"] for spec in blocks.values()}
        self.assertEqual(by_value, {"Statutory Audit", "Expansion of Services"})

    def test_slides_in_every_deck_stay_in_the_baseline(self):
        self.merge()
        rules = self.rules_json()
        self.assertEqual(len(rules["baseline"]), 6)
        self.assertIn("about_us", rules["baseline"])

    def test_conditional_blocks_are_anchored_where_they_sat(self):
        """Without insert_after the builder can only append, and the deck comes
        back in the wrong order."""
        self.merge()
        blocks = self.rules_json()["blocks"]
        anchored = [b for b, s in blocks.items() if s.get("insert_after")]
        self.assertEqual(len(anchored), len(blocks))

    def test_an_anchor_may_be_another_conditional_block(self):
        """Two slides that always travel together keep their order, instead of
        both flattening onto the last unconditional slide."""
        self.merge()
        blocks = self.rules_json()["blocks"]
        chained = [s["insert_after"] for s in blocks.values()
                   if s.get("insert_after") in blocks]
        self.assertTrue(chained, "expected one block to anchor to another")

    # -- rebuilding the rules without re-merging --------------------------
    def _wipe_rules(self, folder):
        """Lose the rules exactly as the real template did, keeping the
        placeholder bindings that tokenising put there."""
        tpl = Template(folder)
        rules = json.load(open(tpl.rules_path, encoding="utf-8"))
        rules["blocks"] = {}
        with tpl.open_library() as lib:
            rules["baseline"] = [b.id for b in lib.ordered()]
        with open(tpl.rules_path, "w", encoding="utf-8") as fh:
            json.dump(rules, fh)
        return tpl

    def test_the_merge_records_where_each_block_sat(self):
        """Library order cannot reproduce it: a block a later deck introduced
        sits at the end of the library wherever it sat in its own deck."""
        self.merge("--tokenise")
        prov = json.load(open(os.path.join(self.roots, "merged",
                                           "provenance.json"), encoding="utf-8"))
        anchored = {b: i.get("after") for b, i in prov["blocks"].items()
                    if i.get("after")}
        self.assertTrue(anchored)

    def test_rules_can_be_rebuilt_from_provenance_alone(self):
        """Re-running the merge would rebuild a library PowerPoint has only
        just agreed to open. The evidence is already on disk."""
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        before = json.load(open(Template(folder).rules_path, encoding="utf-8"))
        self._wipe_rules(folder)

        report = propose.propose_from_provenance(Template(folder), self.pays)
        after = json.load(open(Template(folder).rules_path, encoding="utf-8"))
        self.assertEqual(sorted(after["blocks"]), sorted(before["blocks"]))
        self.assertEqual(after["baseline"], before["baseline"])
        self.assertEqual(report["conditional"], len(before["blocks"]))

    def test_rebuilding_the_rules_keeps_the_placeholder_bindings(self):
        """They come from tokenising the library and have nothing to do with
        slide selection. Losing them turns a template back into a snapshot."""
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        bound = json.load(open(Template(folder).rules_path,
                               encoding="utf-8"))["placeholders"]
        self.assertTrue(bound)
        self._wipe_rules(folder)
        propose.propose_from_provenance(Template(folder), self.pays)
        after = json.load(open(Template(folder).rules_path, encoding="utf-8"))
        self.assertEqual(after["placeholders"], bound)

    def test_rebuilt_anchors_come_from_the_decks_not_library_order(self):
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        self._wipe_rules(folder)
        report = propose.propose_from_provenance(Template(folder), self.pays)
        self.assertEqual(report["anchors_guessed"], 0)
        blocks = json.load(open(Template(folder).rules_path,
                                encoding="utf-8"))["blocks"]
        for spec in blocks.values():
            self.assertNotIn("_confirm_position", spec)

    def test_a_rebuilt_template_still_builds_the_right_deck(self):
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        self._wipe_rules(folder)
        propose.propose_from_provenance(Template(folder), self.pays)
        built = build_template(Template(folder), self.cases["01_expansion"],
                               self.out("rebuilt_rules.pptx"))
        self.assertEqual(built["slides"], 8, "not every slide, only the right ones")

    def test_anchors_can_be_recovered_from_the_decks(self):
        """provenance.json from an older merge has no anchors. The decks still
        know the order, and reading them is far cheaper than rebuilding the
        library - which is the part that is hard to get right."""
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")

        prov_path = os.path.join(folder, "provenance.json")
        prov = json.load(open(prov_path, encoding="utf-8"))
        for info in prov["blocks"].values():
            info.pop("after", None)                    # the older shape
        with open(prov_path, "w", encoding="utf-8") as fh:
            json.dump(prov, fh)
        self._wipe_rules(folder)

        without = propose.propose_from_provenance(Template(folder), self.pays)
        self._wipe_rules(folder)
        with_decks = propose.propose_from_provenance(Template(folder), self.pays,
                                                     decks_dir=self.decks)
        self.assertGreater(with_decks["anchors_from_decks"],
                           without["anchors_from_decks"],
                           "reading the decks must recover positions")

    def test_geometry_is_only_trusted_when_it_names_one_block(self):
        """Slides built from one layout share their geometry. Matching on it
        anyway would put a block in the wrong place, quietly - so an ambiguous
        geometry must match nothing at all."""
        from engine import propose
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        orders, unreadable, matching = propose.recover_anchors(
            Template(folder), self.decks)
        self.assertEqual(unreadable, [])
        for order in orders:
            self.assertEqual(len(order), len(set(order)),
                             "a block matched twice means a wrong match")

    # -- does it match Templafy? -----------------------------------------
    def test_our_decks_match_the_ones_templafy_produced(self):
        """The acceptance test for the whole exercise: same slides, same order,
        for the same answers."""
        from engine import verify as verifymod
        self.merge("--tokenise")
        report = verifymod.verify(Template(os.path.join(self.roots, "merged")),
                                  self.decks, self.pays)
        self.assertEqual(report["compared"], len(self.cases))
        self.assertEqual(report["exact"], report["compared"],
                         [r["verdict"] for r in report["results"]])

    def test_a_broken_rule_is_caught(self):
        """A check that cannot fail proves nothing - the first version of this
        module reported differences that were not there, and missed real ones."""
        from engine import verify as verifymod
        self.merge("--tokenise")
        folder = os.path.join(self.roots, "merged")
        tpl = Template(folder)
        rules = json.load(open(tpl.rules_path, encoding="utf-8"))
        victim = sorted(rules["blocks"])[0]
        rules["blocks"][victim]["when"] = {"field": "AuditType",
                                           "eq": "Never Matches Anything"}
        with open(tpl.rules_path, "w", encoding="utf-8") as fh:
            json.dump(rules, fh)

        report = verifymod.verify(Template(folder), self.decks, self.pays)
        self.assertLess(report["selection_ok"], report["compared"])
        broken = [r for r in report["results"] if not r["selection_ok"]]
        self.assertTrue(any(r["missing"] for r in broken))

    def test_slides_are_matched_one_to_one(self):
        """The bug that made the first version useless: keying a dictionary on
        slide identity collapsed every slide sharing an identity onto one, so
        it reported differences that did not exist."""
        from engine import forensics, verify as verifymod
        self.merge("--tokenise")
        library = os.path.join(self.roots, "merged", "library.pptx")
        deck = forensics.load_deck(os.path.join(self.decks, "00_baseline.pptx"))
        lib = forensics.load_deck(library)
        pairs, _only_a, _only_b = verifymod.align(deck["slides"], lib["slides"])
        matched = [b["index"] for _a, b, _m, _s in pairs]
        self.assertEqual(len(matched), len(set(matched)),
                         "a library slide was matched to two deck slides")

    def test_a_deck_matches_itself(self):
        """The floor: if this fails nothing else about the comparison means
        anything."""
        from engine import verify as verifymod
        deck = os.path.join(self.decks, "00_baseline.pptx")
        result = verifymod.compare(deck, deck)
        self.assertEqual(result["verdict"], "MATCH")
        self.assertTrue(result["order_ok"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["extra"], [])

    # -- tokenising -------------------------------------------------------
    def test_without_tokenising_the_library_is_one_client_snapshot(self):
        """Generated decks have their values already substituted. Saying so is
        the point — a silent snapshot would look like a working template."""
        self.merge()
        with self.merged().open_library() as lib:
            self.assertEqual(lib.all_placeholders(), set())
            text = " ".join(A_T.findall(lib.read(p)) and
                            " ".join(A_T.findall(lib.read(p)))
                            for p in lib.slide_parts)
        self.assertIn("Example Corporation", text)

    def test_tokenising_puts_the_placeholders_back(self):
        self.merge("--tokenise")
        with self.merged().open_library() as lib:
            found = lib.all_placeholders()
            text = " ".join(" ".join(A_T.findall(lib.read(p)))
                            for p in lib.slide_parts)
        self.assertIn("ClientName", found)
        self.assertIn("DueDate", found)
        self.assertNotIn("Example Corporation", text)
        self.assertNotIn("November 30, 2026", text)

    def test_the_date_rendering_is_remembered_in_the_binding(self):
        """The deck showed "November 30, 2026"; without recording which format
        that was, a rebuild emits a raw 20261130."""
        self.merge("--tokenise")
        binding = self.rules_json()["placeholders"]["{{DueDate}}"]
        self.assertEqual(binding["field"], "DueDate")
        self.assertEqual(binding["format"], "long_comma")

    def test_tokenising_matches_whole_words_only(self):
        """v1 §2: "Example" must not turn "Examples" into "{{ShortClientName}}s"."""
        reps = [("Example", "{{Short}}", "ShortClientName", None)]
        xml = "<a:t>Examples of Example work</a:t>"
        out, hits, _fmt = self.bmd.tokenise_part(xml, reps)
        self.assertIn("Examples of {{Short}} work", out)
        self.assertEqual(hits["Example"], 1)

    def test_tokenising_never_touches_markup(self):
        reps = [("Example Corporation", "{{ClientName}}", "FullClientName", None)]
        tbl = ('<a:tbl><a:tr><a:tc><a:txBody><a:p><a:r>'
               '<a:t>Example Corporation</a:t></a:r></a:p></a:txBody></a:tc>'
               '</a:tr></a:tbl>')
        out, _hits, _fmt = self.bmd.tokenise_part(tbl, reps)
        self.assertIn("<a:tbl>", out)
        self.assertIn("<a:t>{{ClientName}}</a:t>", out)

    # -- the whole loop ---------------------------------------------------
    def test_round_trip_builds_a_deck_for_a_different_client(self):
        """Templafy decks -> master library -> a new client's deck. This is the
        point of the whole exercise."""
        self.merge("--tokenise")
        answers = {"FullClientName": "Acme Holdings plc", "ShortClientName": "Acme",
                   "DueDate": "20270601", "City": "Leeds",
                   "AuditType": "Expansion of Services"}
        report = build_template(self.merged(), answers,
                                self.out("merged_roundtrip.pptx"))
        text = _deck_text(report["out"])
        self.assertIn("Acme Holdings plc", text)
        self.assertIn("June 1, 2027", text)
        self.assertIn("Leeds", text)
        for leaked in ("Example Corporation", "November 30, 2026", "New York",
                       "20270601", "{{"):
            self.assertNotIn(leaked, text, "%r leaked into the deck" % leaked)
        self.assertEqual(report["unfilled_placeholders"], [])
        v = _validator()
        if v:
            self.assertEqual(v.validate(report["out"]), [])

    def test_round_trip_reproduces_the_original_slide_order(self):
        self.merge("--tokenise")
        answers = dict(self.cases["01_expansion"])
        report = build_template(self.merged(), answers,
                                self.out("merged_order.pptx"))
        original = zipfile.ZipFile(os.path.join(self.decks, "01_expansion.pptx"))
        expected = [" ".join(A_T.findall(original.read(n).decode("utf-8")))
                    for n in _slide_parts(original)]
        rebuilt = zipfile.ZipFile(report["out"])
        actual = [" ".join(A_T.findall(rebuilt.read(n).decode("utf-8")))
                  for n in _slide_parts(rebuilt)]
        self.assertEqual(len(actual), len(expected))
        # same slides, same order (text is identical: same answers)
        self.assertEqual(actual, expected)


# ---------------------------------------------------------------- tokenise
class TestTokeniseLibrary(EngineTestCase):
    """Restoring placeholders in a library that is already built.

    The real template was merged without --tokenise, so its slides carry one
    client's details as literal text. Re-merging to fix that would rebuild a
    package PowerPoint had only just agreed to open, so the repair has to work
    on the library as it stands.
    """

    def setUp(self):
        self.payload = {"FullClientName": "Example Corporation",
                        "ShortClientName": "Example", "DueDate": "20261130",
                        "City": "New York", "AuditType": "Statutory Audit"}
        self.folder = os.path.join(
            tempfile.mkdtemp(prefix="pptgen2_tok_", dir=self.tmp), "snap")
        shutil.copytree(DEMO, self.folder,
                        ignore=shutil.ignore_patterns("report.md"))
        # a library with the values already filled in, like a generated deck
        build(LIBRARY, self.rules, self.payload,
              os.path.join(self.folder, "library.pptx"), data_sources=self.data)
        with open(os.path.join(self.folder, "template.json"), "w") as fh:
            json.dump({"name": "snap", "library": "library.pptx"}, fh)
        # The built deck's markers are stripped, so its ids would come from
        # titles - and tokenising rewrites the very text a title is read from.
        # Pin them in a sidecar first, exactly as the real template does.
        tpl = Template(self.folder)
        with tpl.open_library() as lib:
            tpl.write_block_map({os.path.basename(b.part):
                                 {"id": b.id, "title": b.title}
                                 for b in lib.ordered()})
            baseline = [b.id for b in lib.ordered()]
        with open(os.path.join(self.folder, "rules.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"name": "snap", "baseline": baseline, "blocks": {},
                       "placeholders": {}}, fh)

    def test_a_filled_in_library_has_no_placeholders_to_begin_with(self):
        with Template(self.folder).open_library() as lib:
            self.assertEqual(lib.all_placeholders(), set())

    def test_tokenising_puts_them_back(self):
        report = tokenisemod.tokenise_library(Template(self.folder), self.payload)
        with Template(self.folder).open_library() as lib:
            found = lib.all_placeholders()
        self.assertIn("ClientName", found)
        self.assertIn("DueDate", found)
        self.assertIn("City", found)
        self.assertGreater(report["slides_changed"], 0)

    def test_the_deck_is_still_valid_afterwards(self):
        """Only text inside <a:t> is touched; everything else is copied
        through, so a package PowerPoint accepts stays acceptable."""
        tokenisemod.tokenise_library(Template(self.folder), self.payload)
        from engine import validate as validator
        self.assertEqual(
            validator.validate(os.path.join(self.folder, "library.pptx")), [])

    def test_the_previous_library_is_kept(self):
        report = tokenisemod.tokenise_library(Template(self.folder), self.payload)
        self.assertTrue(os.path.exists(report["backup"]))

    def test_the_date_rendering_is_recorded_for_the_binding(self):
        report = tokenisemod.tokenise_library(Template(self.folder), self.payload)
        self.assertEqual(report["formats"].get("DueDate"), "long_comma")
        bindings = tokenisemod.bindings_for(report["map"], report["formats"])
        self.assertEqual(bindings["{{DueDate}}"],
                         {"from": "field", "field": "DueDate",
                          "format": "long_comma"})

    def test_whole_words_only(self):
        """v1 §2: "Example" must not turn "Examples" into a placeholder."""
        reps = tokenisemod.replacements({"ShortClientName": "Example"},
                                        {"ShortClientName": "Short"})
        out, hits, _f = tokenisemod.tokenise_part(
            "<a:t>Examples of Example work</a:t>", reps)
        self.assertIn("Examples of {{Short}} work", out)
        self.assertEqual(hits["Example"], 1)

    def test_a_round_trip_produces_a_different_client(self):
        """The point of the exercise."""
        tpl = Template(self.folder)
        report = tokenisemod.tokenise_library(tpl, self.payload)
        rules = json.load(open(tpl.rules_path, encoding="utf-8"))
        rules["placeholders"] = tokenisemod.bindings_for(report["map"],
                                                         report["formats"])
        with open(tpl.rules_path, "w", encoding="utf-8") as fh:
            json.dump(rules, fh)

        built = build_template(Template(self.folder),
                               {"FullClientName": "Acme Holdings plc",
                                "ShortClientName": "Acme", "DueDate": "20270601",
                                "City": "Leeds", "AuditType": "Statutory Audit"},
                               self.out("tokenised_roundtrip.pptx"))
        text = _deck_text(built["out"])
        self.assertIn("Acme Holdings plc", text)
        self.assertIn("June 1, 2027", text)
        self.assertIn("Leeds", text)
        for leaked in ("Example Corporation", "November 30, 2026", "New York"):
            self.assertNotIn(leaked, text, "%r survived" % leaked)

    def test_recorded_titles_are_refreshed(self):
        """Substitution rewrites the text a title is read from, so leaving the
        sidecar alone would make every tokenised slide report drift straight
        afterwards - burying real warnings under self-inflicted ones."""
        report = tokenisemod.tokenise_library(Template(self.folder), self.payload)
        self.assertGreater(report["retitled"], 0)
        with Template(self.folder).open_library() as lib:
            self.assertEqual(lib.map_drift(), [])

    def test_the_wrong_payload_changes_nothing(self):
        """Better to replace nothing and say so than to mangle the deck."""
        report = tokenisemod.tokenise_library(
            Template(self.folder),
            {"FullClientName": "Some Other Company", "DueDate": "19990101"})
        self.assertEqual(report["replacements"], {})
        self.assertEqual(report["slides_changed"], 0)


# ---------------------------------------------------------------- evidence
_CREATION_ID = ('<a:extLst><a:ext uri="{FF2B5EF4-FFF2-40B4-BE49-F238E27FC236}">'
                '<a16:creationId xmlns:a16="http://schemas.microsoft.com/office/'
                'drawing/2014/main" id="{%s}"/></a:ext></a:extLst>')


class TestTokeniseEvidence(EngineTestCase):
    """Which literals are really dynamic, decided by the decks rather than
    guessed.

    On the real template, tokenising replaced "New York" everywhere it appeared.
    Two of those were a static office address, so every city deck then said
    "Atlanta" where Templafy correctly says "New York". v1 warned about exactly
    this; the 70 generated decks settle it.
    """

    def setUp(self):
        work = tempfile.mkdtemp(prefix="pptgen2_ev_", dir=self.tmp)
        self.decks = os.path.join(work, "decks")
        self.pays = os.path.join(work, "payloads")
        self.folder = os.path.join(work, "firm")
        os.makedirs(self.decks)
        os.makedirs(self.pays)
        shutil.copytree(DEMO, self.folder,
                        ignore=shutil.ignore_patterns("report.md"))

        # a library where each slide carries a creationId, and one slide has a
        # STATIC "New York" office line as well as the dynamic {{City}} slide
        authored = os.path.join(work, "authored.pptx")
        z = zipfile.ZipFile(LIBRARY)
        with zipfile.ZipFile(authored, "w") as out:
            for info in z.infolist():
                data = z.read(info.filename)
                m = re.match(r'ppt/slides/slide(\d+)\.xml$', info.filename)
                if m:
                    n = int(m.group(1))
                    extra = ("Our New York office" if n == 2
                             else "marker %d" % n)
                    data = z.read(info.filename).decode().replace(
                        "</p:spTree>",
                        '<p:sp><p:nvSpPr><p:cNvPr id="900" name="cid">%s</p:cNvPr>'
                        '<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody>'
                        '<a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>%s</a:t></a:r>'
                        '</a:p></p:txBody></p:sp></p:spTree>'
                        % (_CREATION_ID % ("%08d-0000-0000-0000-000000000000" % n),
                           extra), 1).encode()
                out.writestr(info, data)
        z.close()

        self.cities = [("00_ny", "New York"), ("01_atlanta", "Atlanta"),
                       ("02_boston", "Boston")]
        for label, city in self.cities:
            payload = {"FullClientName": "Example Corporation",
                       "ShortClientName": "Example", "DueDate": "20261130",
                       "City": city, "AuditType": "Statutory Audit"}
            with open(os.path.join(self.pays, label + ".json"), "w") as fh:
                json.dump(payload, fh)
            build(authored, self.rules, payload,
                  os.path.join(self.decks, label + ".pptx"),
                  data_sources=self.data)

        # the library is a SNAPSHOT: the New York deck, values already filled
        shutil.copy(os.path.join(self.decks, "00_ny.pptx"),
                    os.path.join(self.folder, "library.pptx"))
        with open(os.path.join(self.folder, "template.json"), "w") as fh:
            json.dump({"name": "firm", "library": "library.pptx"}, fh)
        self.payload = json.load(
            open(os.path.join(self.pays, "00_ny.json"), encoding="utf-8"))

    def classify(self):
        tpl = Template(self.folder)
        return tpl, tokenisemod.classify_literals(
            tpl, self.decks, self.pays, tokenisemod.guess_token_map(self.payload))

    def _text(self):
        with Template(self.folder).open_library() as lib:
            return {b.id: " ".join(A_T.findall(lib.read(b.part)))
                    for b in lib.ordered()}

    def _block_with(self, needle):
        """Block ids come from slide titles in a snapshot, so find the slide by
        what is on it rather than by a name it may not have."""
        for bid, text in self._text().items():
            if needle in text:
                return bid
        raise AssertionError("no slide contains %r" % needle)

    def test_a_slide_that_tracks_the_answer_is_dynamic(self):
        _tpl, cls = self.classify()
        bid = self._block_with("Local office")
        self.assertEqual(cls[(bid, "City")]["verdict"], "dynamic")

    def test_a_static_office_address_is_not_dynamic(self):
        """It contains "New York" only in the deck whose answer was also New
        York - which is what makes it look dynamic if you do not check."""
        _tpl, cls = self.classify()
        bid = self._block_with("Our New York office")
        about = cls[(bid, "City")]
        self.assertIn(about["verdict"], ("static", "mixed"))
        self.assertLess(about["hits"], about["decks"])

    def test_only_the_dynamic_occurrence_is_replaced(self):
        tpl, cls = self.classify()
        contacts = self._block_with("Local office")
        about = self._block_with("Our New York office")
        tokenisemod.tokenise_library(tpl, self.payload, classification=cls)
        text = self._text()
        self.assertIn("{{City}}", text[contacts], "the real city should be a slot")
        self.assertIn("New York", text[about], "the office address must survive")
        self.assertNotIn("{{City}}", text[about])

    def test_without_evidence_everything_is_replaced(self):
        """The old behaviour, and why the evidence matters: the static office
        address becomes a placeholder too."""
        about = self._block_with("Our New York office")
        tokenisemod.tokenise_library(Template(self.folder), self.payload)
        text = self._text()
        self.assertIn("{{City}}", text[about])
        self.assertNotIn("New York", text[about])


# ---------------------------------------------------------------- check
class TestReport(EngineTestCase):
    """`build.py check` is how a problem reaches us from a machine we cannot
    see, so it has to catch real faults and never leak client text."""

    def report(self, template, **kw):
        kw.setdefault("validator", _validator())
        return reportmod.check(template, **kw)

    def test_a_healthy_template_reports_clean(self):
        rep = self.report(Template(DEMO))
        self.assertEqual(rep.of(reportmod.ERROR), [])
        self.assertEqual(rep.of(reportmod.WARNING), [])
        self.assertTrue(rep.ok)

    def test_codes_are_stable_identifiers(self):
        """The console and any future UI key off these, not the prose."""
        rep = self.report(Template(DEMO))
        for finding in rep.findings:
            self.assertRegex(finding.code, r'^[a-z][a-z0-9-]+$')

    def test_missing_rules_is_an_error(self):
        folder = os.path.join(self.tmp, "norules")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns(
            "rules.json", "report.md", ".thumbs"))
        rep = self.report(Template(folder), sample=False)
        self.assertIn("no-rules", [f.code for f in rep.of(reportmod.ERROR)])

    def test_an_unbound_placeholder_is_an_error(self):
        folder = os.path.join(self.tmp, "unbound")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("report.md"))
        rules = json.load(open(os.path.join(folder, "rules.json"), encoding="utf-8"))
        rules["placeholders"].pop("{{ClientName}}")
        json.dump(rules, open(os.path.join(folder, "rules.json"), "w",
                              encoding="utf-8"))
        rep = self.report(Template(folder))
        codes = [f.code for f in rep.of(reportmod.ERROR)]
        self.assertIn("unbound-placeholder", codes)

    def test_rules_naming_a_missing_block_is_an_error(self):
        folder = os.path.join(self.tmp, "ghost")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("report.md"))
        rules = json.load(open(os.path.join(folder, "rules.json"), encoding="utf-8"))
        rules["baseline"].append("no_such_block")
        json.dump(rules, open(os.path.join(folder, "rules.json"), "w",
                              encoding="utf-8"))
        rep = self.report(Template(folder), sample=False)
        self.assertIn("rules-mismatch", [f.code for f in rep.of(reportmod.ERROR)])

    def test_a_template_with_no_placeholders_is_flagged(self):
        """The merge trap: a library built from generated decks substitutes
        nothing, so every client gets the same deck."""
        folder = os.path.join(self.tmp, "nophs")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("report.md"))
        # a library whose slides carry no {{...}} at all
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import make_demo_library as mk
        original = mk.build_slides

        def plain():
            # stitch first: the contacts slide authors {{City}} across four
            # runs, so a regex over the raw XML would miss it and the library
            # would not actually be placeholder-free
            return [(bid, re.sub(r'\{\{[A-Za-z0-9_.]+\}\}', 'Acme',
                                 placeholders.normalise_runs(xml)))
                    for bid, xml in original()]
        try:
            mk.build_slides = plain
            mk.write_library(os.path.join(folder, "library.pptx"))
        finally:
            mk.build_slides = original
        rules = json.load(open(os.path.join(folder, "rules.json"), encoding="utf-8"))
        rules["placeholders"] = {}
        json.dump(rules, open(os.path.join(folder, "rules.json"), "w",
                              encoding="utf-8"))

        rep = self.report(Template(folder))
        self.assertIn("no-placeholders",
                      [f.code for f in rep.of(reportmod.WARNING)])

        # with provenance.json present it is the merge bug, and an error
        json.dump({"deck_count": 2, "blocks": {}},
                  open(os.path.join(folder, "provenance.json"), "w"))
        rep = self.report(Template(folder))
        error = next(f for f in rep.of(reportmod.ERROR)
                     if f.code == "no-placeholders")
        self.assertIn("build.py tokenise", error.detail,
                      "the report must name the command that fixes it")

    def test_a_merge_that_did_not_merge_is_an_error(self):
        """The real 70-deck run filed this as a note at the bottom, under the
        one finding that mattered. It is the loudest signal there is."""
        folder = os.path.join(self.tmp, "nomerge")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("report.md"))
        with open(os.path.join(folder, "provenance.json"), "w") as fh:
            json.dump({"deck_count": 70,
                       "blocks": {"b%d" % i: {"deck_count": 1} for i in range(500)}},
                      fh)
        rep = self.report(Template(folder), sample=False)
        self.assertIn("merge-did-not-merge",
                      [f.code for f in rep.of(reportmod.ERROR)])

    def test_a_healthy_merge_is_not_flagged(self):
        folder = os.path.join(self.tmp, "goodmerge")
        shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("report.md"))
        with open(os.path.join(folder, "provenance.json"), "w") as fh:
            json.dump({"deck_count": 70,
                       "blocks": {"b%d" % i: {"deck_count": 40}
                                  for i in range(120)}}, fh)
        rep = self.report(Template(folder), sample=False)
        self.assertNotIn("merge-did-not-merge",
                         [f.code for f in rep.of(reportmod.ERROR)])

    def test_repeated_findings_are_grouped(self):
        """136 copies of one sentence made the real report unreadable - it had
        to be trimmed by hand before it could be sent."""
        rep = reportmod.Report(Template(DEMO))
        for i in range(136):
            rep.add(reportmod.WARNING, "sidecar-drift",
                    "slide%d.xml changed" % i, "detail")
        doc = reportmod.to_markdown(rep, include_blocks=False)
        self.assertIn("## Warnings (136)", doc)
        self.assertEqual(sum(1 for l in doc.splitlines()
                             if l.startswith("### ")), 1)
        self.assertIn("136 occurrences", doc)
        self.assertIn("and 121 more", doc)

    def test_unstable_ids_are_warned_about(self):
        folder = os.path.join(self.tmp, "loose")
        os.makedirs(folder)
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import make_demo_library as mk
        was, mk.MARKERS = mk.MARKERS, False
        try:
            mk.write_library(os.path.join(folder, "library.pptx"))
        finally:
            mk.MARKERS = was
        rep = self.report(Template(folder), sample=False)
        self.assertIn("unstable-ids", [f.code for f in rep.of(reportmod.WARNING)])

    def test_a_broken_library_reports_rather_than_crashing(self):
        folder = os.path.join(self.tmp, "broken")
        os.makedirs(folder)
        with zipfile.ZipFile(os.path.join(folder, "library.pptx"), "w") as z:
            z.writestr("hello.txt", "not a deck")
        rep = self.report(Template(folder))
        self.assertIn("library-unreadable",
                      [f.code for f in rep.of(reportmod.ERROR)])

    # -- the document -----------------------------------------------------
    def test_markdown_leads_with_the_verdict(self):
        rep = self.report(Template(DEMO))
        doc = reportmod.to_markdown(rep)
        self.assertTrue(doc.startswith("# Template check - demo"))
        self.assertIn("## Summary", doc)
        self.assertIn("## Blocks", doc)

    def test_redacted_report_carries_no_slide_text(self):
        """The promise on the page is that this is safe to send outside the
        firm, so nothing client-identifying may survive - block ids included,
        since they are derived from slide titles."""
        rep = self.report(Template(DEMO), redact=True)
        doc = reportmod.to_markdown(rep)
        with Library(LIBRARY) as lib:
            titles = [b.title for b in lib.ordered() if b.title]
            ids = [b.id for b in lib.ordered()]
        for title in titles:
            self.assertNotIn(title, doc, "slide title leaked")
        for bid in ids:
            self.assertNotIn(bid, doc, "block id leaked (derived from a title)")
        self.assertIn("block_01", doc, "ids must still be referable")

    def test_redaction_keeps_every_finding(self):
        plain = self.report(Template(DEMO))
        hidden = self.report(Template(DEMO), redact=True)
        self.assertEqual([f.code for f in plain.sorted_findings()],
                         [f.code for f in hidden.sorted_findings()])

    def test_unredacted_report_warns_before_sharing(self):
        doc = reportmod.to_markdown(self.report(Template(DEMO)))
        self.assertIn("Before sharing", doc)
        self.assertIn("--redact", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
