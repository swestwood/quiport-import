"""Unit tests. Stdlib unittest, no fixtures required:

    python3 -m unittest discover -s tests -v

Cases marked "regression" encode a behaviour measured against the live API and
documented in PROBE-FINDINGS.md. If one of those starts failing, the API has
changed, not the test.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import json
import pathlib
import tempfile
import time
import unittest
from pathlib import Path

from quiport import htmlutil as H
from quiport import linkpass, naming
from quiport.model import Message, Thread, unwrap
from quiport.rawstore import RawStore, canonical, endpoint_slug
from quiport.render import comments as C
from quiport.render import transform as T
from quiport.render.blobs import sniff_extension
from quiport.render.document import RenderReport


class TestNaming(unittest.TestCase):
    def test_slug_basics(self):
        self.assertEqual(naming.slugify("Weekly Sales Email Plan"), "Weekly-Sales-Email-Plan")
        self.assertEqual(naming.slugify("  spaced   out  "), "spaced-out")
        self.assertEqual(naming.slugify("R&D notes"), "R-and-D-notes")

    def test_slug_unicode_is_folded_not_dropped(self):
        self.assertEqual(naming.slugify("Café Münchén"), "Cafe-Munchen")

    def test_slug_emoji_and_punctuation(self):
        self.assertEqual(naming.slugify("❗️ very important cat pics ❗️"),
                         "very-important-cat-pics")

    def test_slug_length_is_bounded_on_word_boundary(self):
        out = naming.slugify("alpha beta gamma delta epsilon zeta eta theta iota kappa " * 3)
        self.assertLessEqual(len(out), naming.MAX_SLUG)
        self.assertFalse(out.endswith("-"))

    def test_reserved_names_avoided(self):
        self.assertEqual(naming.slugify("CON"), "CON-doc")

    def test_filename_uses_secret_path_prefix(self):
        self.assertEqual(naming.thread_filename("Sp1xmplPath0", "My Doc"),
                         "Sp1xmplPath0-My-Doc.html")

    def test_filename_survives_empty_title(self):
        self.assertEqual(naming.thread_filename("abc123", ""), "abc123-untitled.html")

    def test_relative_paths(self):
        self.assertEqual(naming.relative_path("A/B/x.html", "A/B/y.html"), "y.html")
        self.assertEqual(naming.relative_path("A/B/C/x.html", "A/D/y.html"), "../../D/y.html")
        self.assertEqual(naming.relative_path("x.html", "A/y.html"), "A/y.html")


class TestPathAllocator(unittest.TestCase):
    def setUp(self):
        self.alloc = naming.PathAllocator("Imported from Quip")

    def test_first_folder_wins_and_others_become_stubs(self):
        self.alloc.allocate_folder("f1", "Alpha", None)
        self.alloc.allocate_folder("f2", "Beta", None)
        p1, canon1 = self.alloc.allocate_thread("t1", "sec", "Doc", "f1")
        p2, canon2 = self.alloc.allocate_thread("t1", "sec", "Doc", "f2")
        self.assertTrue(canon1)
        self.assertFalse(canon2)
        self.assertEqual(self.alloc.canonical["t1"], p1)
        self.assertIn(p2, self.alloc.stubs["t1"])

    def test_same_folder_twice_creates_no_stub(self):
        self.alloc.allocate_folder("f1", "Alpha", None)
        self.alloc.allocate_thread("t1", "sec", "Doc", "f1")
        self.alloc.allocate_thread("t1", "sec", "Doc", "f1")
        self.assertEqual(self.alloc.stubs.get("t1", []), [])

    def test_sibling_folders_with_same_title_disambiguate(self):
        a = self.alloc.allocate_folder("f1", "Notes", None)
        b = self.alloc.allocate_folder("f2", "Notes", None)
        self.assertNotEqual(a, b)


class TestHtmlParser(unittest.TestCase):
    """regression: neither API version returns XML-parseable HTML."""

    def test_valueless_attribute_survives(self):
        src = "<td data-live-app-process-bar-step-selected>Complete</td>"
        tree = H.parse(src)
        td = next(H.iter_elements(tree, "td"))
        self.assertIn("data-live-app-process-bar-step-selected", td.attrs)
        self.assertIsNone(td.attrs["data-live-app-process-bar-step-selected"])
        self.assertIn("data-live-app-process-bar-step-selected", H.serialize(tree))

    def test_bare_br_does_not_break_the_tree(self):
        tree = H.parse("<p>one<br>two<br/>three</p>")
        self.assertEqual(tree.text(), "onetwothree")
        self.assertEqual(len(list(H.iter_elements(tree, "br"))), 2)

    def test_entities_round_trip(self):
        src = "<p>a &amp; b &#x27;c&#x27; &nbsp;</p>"
        self.assertEqual(H.serialize(H.parse(src)), src)

    def test_unclosed_tags_do_not_lose_content(self):
        tree = H.parse("<div><p>kept<div>also kept")
        self.assertIn("kept", tree.text())
        self.assertIn("also kept", tree.text())

    def test_stray_end_tag_is_ignored(self):
        tree = H.parse("<p>text</span></p>")
        self.assertEqual(tree.text(), "text")

    def test_serialization_is_idempotent(self):
        src = "<div id='a' class='line'><b>x</b><br>y</div>"
        once = H.serialize(H.parse(src))
        twice = H.serialize(H.parse(once))
        self.assertEqual(once, twice)

    def test_roundtrip_check_reports_ok_for_real_shapes(self):
        src = ("<h1 id='a'>Title</h1><p id='b' class='line'>Body &amp; more</p>"
               "<div data-section-style='7'><ul><li class='checked'>done</li></ul></div>")
        ok, note = H.roundtrip_ok(src)
        self.assertTrue(ok, note)

    def test_find_by_id_and_append(self):
        tree = H.parse("<p id='x'>hi</p>")
        el = H.find_by_id(tree, "x")
        self.assertIsNotNone(el)
        el.append(H.Text("!"))
        self.assertIn("hi!", H.serialize(tree))


class TestTransforms(unittest.TestCase):
    def setUp(self):
        self.report = RenderReport("t", "T")

    def test_checklist_becomes_real_checkboxes(self):
        src = ("<div data-section-style='7'><ul>"
               "<li class='checked'>done</li><li>todo</li></ul></div>")
        tree = H.parse(src)
        n = T.render_checklists(tree)
        out = H.serialize(tree)
        self.assertEqual(n, 2)
        self.assertIn("checked", out)
        self.assertEqual(out.count("<input"), 2)
        self.assertEqual(out.count('checked>'), 1)  # only the completed one

    def test_checklist_parent_rows_are_not_tasks(self):
        src = "<div data-section-style='7'><ul><li class='parent'>group</li></ul></div>"
        tree = H.parse(src)
        self.assertEqual(T.render_checklists(tree), 0)

    def test_blob_rewriting_accepts_both_url_forms(self):
        """regression: V1 emits a relative /blob/ URL, V2 an absolute one."""
        src = ("<img src='/blob/T1/B1'>"
               "<img src='https://quip.com/blob/T1/B2'>")
        tree = H.parse(src)
        seen = []

        def resolver(tid, bid, el, page, name_hint=""):
            seen.append((tid, bid))
            return f"_files/{bid}.png"

        T.rewrite_blobs(tree, resolver, "a/b.html", self.report)
        self.assertEqual(seen, [("T1", "B1"), ("T1", "B2")])
        self.assertNotIn("/blob/", H.serialize(tree))

    def test_empty_image_well_becomes_a_small_box(self):
        """Quip's unfilled image well is a 1x1 spacer sized 800x600."""
        tree = H.parse(
            "<div data-section-style='11'><div class='empty'>"
            "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=='"
            " width='800' height='600' class='image'></div></div>")
        report = RenderReport("t", "T")
        self.assertEqual(T.render_image_placeholders(tree, report), 1)
        out = H.serialize(tree)
        self.assertIn("quip-image-placeholder", out)
        self.assertIn("+ Add an image", out)
        self.assertIn('data-quip-placeholder-size="800x600"', out)
        self.assertNotIn("<img", out)

    def test_placeholder_is_not_flagged_for_human_attention(self):
        """Nothing was lost -- the well is empty in Quip too."""
        tree = H.parse("<div class='empty'><img src='data:image/gif;base64,R0lGODlh'></div>")
        report = RenderReport("t", "T")
        T.render_image_placeholders(tree, report)
        self.assertTrue(report.has("image_placeholder"))
        self.assertFalse(report.has("missing_blob"))

    def test_placeholder_label_is_registered_as_generated_markup(self):
        """Its text abuts the next line, fusing 'image' + 'Check' into one word.

        Caught by content_preserved on a real document, exactly as the comment
        marker was. Any element this pipeline adds inside .quip-body has to be
        strippable, or it corrupts the comparison against raw.
        """
        from quiport.verify import GENERATED_CLASSES
        self.assertIn("quip-image-placeholder", GENERATED_CLASSES)

    def test_real_images_are_untouched(self):
        """regression: a real image always has a /blob/ src, never a data URI."""
        tree = H.parse("<div data-section-style='11'>"
                       "<img src='https://quip.com/blob/T1/B1' alt='photo.png'></div>")
        report = RenderReport("t", "T")
        self.assertEqual(T.render_image_placeholders(tree, report), 0)
        self.assertIn("/blob/T1/B1", H.serialize(tree))

    def test_a_large_inline_image_is_kept_and_reported(self):
        """Not every data URI is a spacer; a real one must not be discarded."""
        tree = H.parse("<p><img src='data:image/png;base64,%s'></p>" % ("A" * 900))
        report = RenderReport("t", "T")
        self.assertEqual(T.render_image_placeholders(tree, report), 0)
        self.assertTrue(report.has("inline_data_image"))

    def test_unfetchable_image_still_reaches_the_attention_list(self):
        """The placeholder path must not swallow an image we genuinely lost."""
        tree = H.parse("<div class='empty'><img src='/blob/T1/B1' alt='real.png'></div>")
        report = RenderReport("t", "T")
        T.rewrite_blobs(tree, lambda *a, **k: None, "doc.html", report)
        T.render_image_placeholders(tree, report)
        self.assertTrue(report.has("missing_blob"))
        self.assertFalse(report.has("image_placeholder"))
        self.assertIn("quip-missing-image", H.serialize(tree))

    def test_missing_blob_marker_names_the_kind(self):
        """The source tag settles image-vs-file; only the name is optional."""
        tree = H.parse("<p><img src='/blob/T1/B1'></p>")
        report = RenderReport("t", "T")
        T.rewrite_blobs(tree, lambda *a, **k: None, "doc.html", report)
        out = H.serialize(tree)
        self.assertIn("[missing image]", out)
        self.assertIn('data-quip-missing-kind="image"', out)

    def test_missing_blob_marker_keeps_the_filename_when_there_is_one(self):
        tree = H.parse("<p><img src='/blob/T1/B1' alt='diagram.png'></p>")
        report = RenderReport("t", "T")
        T.rewrite_blobs(tree, lambda *a, **k: None, "doc.html", report)
        self.assertIn("[missing image: diagram.png]", H.serialize(tree))

    def test_missing_linked_file_is_not_called_an_image(self):
        tree = H.parse("<p><a href='/blob/T1/B1'>report.pdf</a></p>")
        report = RenderReport("t", "T")
        T.rewrite_blobs(tree, lambda *a, **k: None, "doc.html", report)
        self.assertIn("[missing file]", H.serialize(tree))

    def test_blob_kind_does_not_collide_with_the_entry_kind(self):
        """regression: RenderReport.add stores the entry type under 'kind', so a
        `kind=` keyword would overwrite it and hide the entry from the reports."""
        tree = H.parse("<p><img src='/blob/T1/B1'></p>")
        report = RenderReport("t", "T")
        T.rewrite_blobs(tree, lambda *a, **k: None, "doc.html", report)
        self.assertEqual(report.count("missing_blob"), 1)
        entry = next(e for e in report.entries if e["kind"] == "missing_blob")
        self.assertEqual(entry["blob_kind"], "image")

    def test_unavailable_blob_becomes_a_visible_placeholder(self):
        """regression: blob permissions are independent of document ones."""
        tree = H.parse("<img src='/blob/T1/B1' alt='chart.png'>")
        T.rewrite_blobs(tree, lambda *a, **k: None, "a.html", self.report)
        out = H.serialize(tree)
        self.assertIn("quip-missing-image", out)
        self.assertIn("chart.png", out)
        self.assertNotIn("<img", out)
        self.assertTrue(self.report.has("missing_blob"))

    def test_live_app_name_comes_from_v1(self):
        """regression: V2 blanks the live-app name; V1 keeps it."""
        v1 = "<div id='s1' data-live-app-id='K1'><details><summary>Kanban board</summary></details></div>"
        v2 = "<div id='s1' data-live-app-id='K1'><details><summary></summary></details></div>"
        names = T.live_app_names_from_v1(v1)
        self.assertEqual(names, {"s1": "Kanban board"})
        tree = H.parse(v2)
        found = T.render_live_apps(tree, names, self.report)
        self.assertEqual(found[0]["name"], "Kanban board")
        self.assertIn("Live app: Kanban board", H.serialize(tree))

    KANBAN = json.dumps({"columns": [
        {"headerContent": "To Do", "cards": [{"content": "First"}]},
        {"headerContent": "Done", "cards": [{"content": "Second"}, {"content": "Third"}]},
    ]})

    def test_quips_own_dump_is_shown_expanded(self):
        """It renders better than anything decoded from the payload."""
        tree = H.parse("<div data-live-app-id='K'><details><summary></summary>"
                       "<table><tr><th>To Do</th></tr></table></details></div>")
        T.render_live_apps(tree, {}, self.report)
        out = H.serialize(tree)
        self.assertIn("<details open", out)
        self.assertIn("quip-liveapp-dump", out)

    def test_payload_table_is_suppressed_when_the_dump_already_shows_it(self):
        """Otherwise every live app renders the same data twice."""
        tree = H.parse(
            f"<div data-live-app-id='K' data-live-app-payload='{self.KANBAN}'>"
            "<details><summary></summary><table><tr><th>To Do</th><th>Done</th></tr>"
            "<tr><td>First</td><td>Second</td></tr><tr><td></td><td>Third</td></tr>"
            "</table></details></div>")
        found = T.render_live_apps(tree, {}, self.report)
        self.assertFalse(found[0]["payload_added"])
        self.assertNotIn("quip-liveapp-kanban", H.serialize(tree))

    def test_payload_table_is_kept_when_the_dump_dropped_content(self):
        """regression: one real Kanban dump rendered its column headers and
        dropped every card, while the payload still had them."""
        tree = H.parse(
            f"<div data-live-app-id='K' data-live-app-payload='{self.KANBAN}'>"
            "<details><summary></summary>"
            "<table><tr><th>To Do</th><th>Done</th></tr></table></details></div>")
        found = T.render_live_apps(tree, {}, self.report)
        self.assertTrue(found[0]["payload_added"])
        out = H.serialize(tree)
        for card in ("First", "Second", "Third"):
            self.assertIn(card, out)
        self.assertTrue(self.report.has("live_app_payload_extra"))

    def test_calendar_payload_is_rendered(self):
        payload = json.dumps({"events": [
            {"content": "Kickoff", "dateRange": {"start": "2021-10-04", "end": "2021-10-04"}}]})
        tree = H.parse(f"<div data-live-app-id='C' data-live-app-payload='{payload}'></div>")
        T.render_live_apps(tree, {}, self.report)
        self.assertIn("Kickoff", H.serialize(tree))

    def test_calendar_has_no_colour_column(self):
        """The dump keeps colour as an inline style; a colour cell no dump can
        contain would make every calendar look like it had something extra."""
        payload = json.dumps({"events": [
            {"content": "Kickoff", "color": "ORANGE",
             "dateRange": {"start": "2021-10-04", "end": "2021-10-04"}}]})
        tree = H.parse(f"<div data-live-app-id='C' data-live-app-payload='{payload}'>"
                       "<details><summary></summary><table><tr>"
                       "<td>2021-10-04</td><td>Kickoff</td></tr></table></details></div>")
        found = T.render_live_apps(tree, {}, self.report)
        self.assertFalse(found[0]["payload_added"])
        out = H.serialize(tree)
        self.assertNotIn("quip-liveapp-calendar", out)
        # ORANGE still appears in the untouched source payload attribute; what
        # must not exist is a rendered cell containing it.
        self.assertNotIn("<td>ORANGE</td>", out)

    def test_tracker_dates_are_readable_not_epoch_milliseconds(self):
        """Quip's dump drops these columns, so the payload is the only copy."""
        payload = json.dumps({
            "columns": [{"id": "0", "content": "**Target Return**"}],
            "rows": [{"0": 1596265200000}]})
        tree = H.parse(f"<div data-live-app-id='P' data-live-app-payload='{payload}'></div>")
        T.render_live_apps(tree, {}, self.report)
        out = H.serialize(tree)
        self.assertIn("2020-08-01", out)
        self.assertNotIn("1596265200000</td>", out)

    def test_small_numbers_are_not_mistaken_for_timestamps(self):
        payload = json.dumps({
            "columns": [{"id": "0", "content": "**Head Count**"}],
            "rows": [{"0": 10560}]})
        tree = H.parse(f"<div data-live-app-id='P' data-live-app-payload='{payload}'></div>")
        T.render_live_apps(tree, {}, self.report)
        self.assertIn("10560", H.serialize(tree))

    def test_unnamed_app_does_not_say_live_app_twice(self):
        tree = H.parse("<div data-live-app-id='X'><details><summary></summary>"
                       "<p>body</p></details></div>")
        T.render_live_apps(tree, {}, self.report)
        self.assertNotIn("Live app: Live app", H.serialize(tree))

    def test_an_app_with_a_dump_is_not_flagged_as_lost(self):
        """Quip's dump usually carries the content, so flagging every live app
        buried the ones that genuinely show nothing."""
        tree = H.parse("<div data-live-app-id='X'><details><summary></summary>"
                       "<table><tr><td>Real content</td></tr></table></details></div>")
        T.render_live_apps(tree, {}, self.report)
        self.assertFalse(self.report.has("live_app_placeholder"))

    def test_live_app_with_nothing_at_all_is_flagged(self):
        tree = H.parse("<div data-live-app-id='X'></div>")
        found = T.render_live_apps(tree, {}, self.report)
        self.assertFalse(found[0]["payload_added"])
        self.assertFalse(found[0]["has_dump"])
        self.assertTrue(self.report.has("live_app_placeholder"))

    def test_malformed_payload_does_not_raise(self):
        tree = H.parse("<div data-live-app-id='X' data-live-app-payload='{not json'></div>")
        found = T.render_live_apps(tree, {}, self.report)
        self.assertFalse(found[0]["payload_added"])


class TestComments(unittest.TestCase):
    def setUp(self):
        self.report = RenderReport("t", "T")
        self.tree = H.parse("<p id='sec1'>first</p><p id='sec2'>second</p>")

    @staticmethod
    def _msg(mid, text, ann=None, sections=(), created=1, documented_key=False):
        rec = {"id": mid, "text": text, "created_usec": created,
               "author_name": "Alex", "visible": True}
        if ann:
            key = "highlighted_section_ids" if documented_key else "highlight_section_ids"
            rec["annotation"] = {"id": ann, key: list(sections)}
        return rec

    def test_uses_the_real_field_name(self):
        """regression: docs say highlighted_section_ids; API says highlight_section_ids."""
        m = Message.from_raw(self._msg("m1", "hi", "a1", ["sec1"]))
        self.assertEqual(m.highlight_section_ids, ["sec1"])

    def test_documented_field_name_still_accepted(self):
        m = Message.from_raw(self._msg("m1", "hi", "a1", ["sec1"], documented_key=True))
        self.assertEqual(m.highlight_section_ids, ["sec1"])

    def test_anchored_comments_are_numbered_in_document_order(self):
        msgs = [self._msg("m1", "on second", "a2", ["sec2"], created=1),
                self._msg("m2", "on first", "a1", ["sec1"], created=2)]
        cs = C.build(msgs, self.tree, self.report)
        self.assertEqual([g["section_id"] for g in cs.anchored], ["sec1", "sec2"])
        self.assertEqual([g["number"] for g in cs.anchored], [1, 2])

    def test_stale_anchors_are_kept_as_unanchored(self):
        """regression: most anchors are stale; dropping them loses ~85% of comments."""
        msgs = [self._msg("m1", "orphan", "a9", ["gone-section"])]
        cs = C.build(msgs, self.tree, self.report)
        self.assertEqual(len(cs.anchored), 0)
        self.assertEqual(len(cs.unanchored), 1)
        self.assertTrue(self.report.has("unanchored_comments"))

    def test_messages_without_annotation_are_chat(self):
        cs = C.build([self._msg("m1", "hello")], self.tree, self.report)
        self.assertEqual(len(cs.chat), 1)
        self.assertEqual(cs.total_groups, 0)

    def test_invisible_messages_are_dropped(self):
        rec = self._msg("m1", "deleted")
        rec["visible"] = False
        cs = C.build([rec], self.tree, self.report)
        self.assertEqual(len(cs.chat), 0)

    def test_replies_group_under_one_footnote(self):
        msgs = [self._msg("m1", "first", "a1", ["sec1"], created=1),
                self._msg("m2", "reply", "a1", ["sec1"], created=2)]
        cs = C.build(msgs, self.tree, self.report)
        self.assertEqual(len(cs.anchored), 1)
        self.assertEqual(len(cs.anchored[0]["messages"]), 2)

    def test_markers_are_inserted_at_the_anchor(self):
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], self.tree, self.report)
        placed = C.insert_markers(self.tree, cs)
        self.assertEqual(placed, 1)
        out = H.serialize(self.tree)
        self.assertIn("quip-comment-ref", out)
        self.assertIn('href="#quip-comment-1"', out)

    def test_marker_is_a_gutter_bubble_carrying_the_comment_count(self):
        msgs = [self._msg("m1", "first", "a1", ["sec1"], created=1),
                self._msg("m2", "reply", "a1", ["sec1"], created=2)]
        cs = C.build(msgs, self.tree, self.report)
        C.insert_markers(self.tree, cs)
        out = H.serialize(self.tree)
        self.assertIn('href="#quip-icon-comment"', out)
        self.assertIn(">2</text>", out)
        self.assertIn('title="2 comments"', out)

    def test_single_comment_is_not_pluralised(self):
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], self.tree, self.report)
        C.insert_markers(self.tree, cs)
        self.assertIn('aria-label="1 comment"', H.serialize(self.tree))

    def test_bubble_leads_its_line_so_it_can_sit_in_the_gutter(self):
        """CSS lifts it left; if it were appended it would trail the text when
        the page is too narrow for a gutter."""
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], self.tree, self.report)
        C.insert_markers(self.tree, cs)
        para = H.serialize(self.tree)
        self.assertLess(para.index("quip-comment-gutter"), para.index("first"))
        self.assertIn("quip-has-comment", para)

    def test_marker_stays_generated_markup_for_verify(self):
        """`verify` strips quip-comment-ref before comparing text against raw;
        without that class the bubble's digit reads as lost source content."""
        from quiport.verify import GENERATED_CLASSES
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], self.tree, self.report)
        C.insert_markers(self.tree, cs)
        self.assertIn("quip-comment-ref", H.serialize(self.tree))
        self.assertIn("quip-comment-ref", GENERATED_CLASSES)

    # -- conversation formatting ------------------------------------------

    def test_the_quoted_passage_replaces_the_footnote_number(self):
        """"1" says nothing once you have scrolled away from the marker."""
        tree = H.parse("<p id='sec1'>The quarterly figures need revisiting.</p>")
        cs = C.build([self._msg("m1", "Agreed", "a1", ["sec1"])], tree, self.report)
        self.assertEqual(cs.anchored[0]["excerpt"],
                         "The quarterly figures need revisiting.")
        html = C.render_comments(cs, lambda *a, **k: None, "doc.html")
        self.assertIn("The quarterly figures need revisiting.", html)
        self.assertIn('href="#quip-comment-ref-1"', html)

    def test_a_long_passage_is_trimmed_on_a_word_boundary(self):
        tree = H.parse(f"<p id='sec1'>{'alpha beta ' * 40}</p>")
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], tree, self.report)
        excerpt = cs.anchored[0]["excerpt"]
        self.assertLessEqual(len(excerpt), C.EXCERPT_CHARS + 1)
        self.assertTrue(excerpt.endswith("…"))
        self.assertFalse(excerpt[:-1].endswith(" "))

    def test_our_own_markers_are_kept_out_of_the_quote(self):
        """The gutter bubble lives inside the anchored element."""
        tree = H.parse("<p id='sec1'>Real text</p>")
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], tree, self.report)
        C.insert_markers(tree, cs)
        cs2 = C.build([self._msg("m1", "x", "a1", ["sec1"])], tree, self.report)
        self.assertEqual(cs2.anchored[0]["excerpt"], "Real text")

    def test_an_anchor_with_no_text_still_links_back(self):
        """An image or empty cell has no quote, but the link must survive."""
        tree = H.parse("<div id='sec1'><img src='x.png'></div>")
        cs = C.build([self._msg("m1", "x", "a1", ["sec1"])], tree, self.report)
        html = C.render_comments(cs, lambda *a, **k: None, "doc.html")
        self.assertIn('href="#quip-comment-ref-1"', html)
        self.assertIn("Comment 1 in the document", html)

    def test_messages_are_not_bullets(self):
        cs = C.build([self._msg("m1", "hello")], self.tree, self.report)
        html = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        self.assertNotIn("<li>", html)
        self.assertIn('class="quip-message"', html)

    def test_name_and_date_are_separate_and_the_exact_time_is_kept(self):
        cs = C.build([self._msg("m1", "Hi!", created=1_600_000_000_000_000)],
                     self.tree, self.report)
        html = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        self.assertIn('<span class="quip-author">Alex</span>', html)
        self.assertIn("quip-timestamp", html)
        self.assertIn("2020", html)
        self.assertRegex(html, r'title="[A-Z][a-z]+ \d+, \d{4} at \d+:\d\d [ap]m"')

    def test_a_dated_rule_marks_each_new_day(self):
        day1, day2 = 1_600_000_000_000_000, 1_600_200_000_000_000
        cs = C.build([self._msg("m1", "first", created=day1),
                      self._msg("m2", "same day", created=day1 + 1000),
                      self._msg("m3", "next day", created=day2)],
                     self.tree, self.report)
        html = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        self.assertEqual(html.count("quip-day-divider"), 2,
                         "one per distinct day, not per message")

    def test_nothing_is_lost_from_a_message(self):
        """Name, date, text, attachment and the link back must all survive."""
        rec = self._msg("m1", "See attached", "a1", ["sec1"], created=1_600_000_000_000_000)
        rec["files"] = [{"hash": "B1", "name": "notes.pdf"}]
        tree = H.parse("<p id='sec1'>Anchored line</p>")
        cs = C.build([rec], tree, self.report)
        html = C.render_comments(cs, lambda *a, **k: "files/notes.pdf", "doc.html")
        for needed in ("Alex", "2020", "See attached", "notes.pdf",
                       "Anchored line", 'href="#quip-comment-ref-1"'):
            self.assertIn(needed, html, needed)

    def test_a_query_string_url_is_escaped_exactly_once(self):
        """regression: the message text is HTML-escaped, then the matched URL
        was escaped *again* for the href -- so `?a=1&b=2` became
        `?a=1&amp;amp;b=2` and the browser requested a literal "&amp;". The
        anchor text still read correctly, so it was invisible until clicked."""
        import html as htmllib
        cs = C.build([self._msg("m1", "see https://example.com/?a=1&b=2&c=3 now")],
                     self.tree, self.report)
        out = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        href = re.search(r'href="([^"]*)"', out).group(1)
        self.assertEqual(htmllib.unescape(href), "https://example.com/?a=1&b=2&c=3")
        self.assertNotIn("&amp;amp;", href)

    def test_the_link_text_stays_readable(self):
        import html as htmllib
        cs = C.build([self._msg("m1", "see https://example.com/?a=1&b=2 now")],
                     self.tree, self.report)
        out = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        shown = re.search(r">(https[^<]*)</a>", out).group(1)
        self.assertEqual(htmllib.unescape(shown), "https://example.com/?a=1&b=2")

    def test_a_url_cannot_break_out_of_the_href_attribute(self):
        """`escape_text` leaves quotes alone, so the attribute escape is the
        only thing standing between a crafted URL and an injected attribute."""
        cs = C.build([self._msg("m1", 'x https://e.com/"onmouseover="alert(1) y')],
                     self.tree, self.report)
        out = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        # Check the parsed element, not the text: quotes in an anchor's *text*
        # are harmless, so a substring search would flag a false positive.
        anchor = next(iter(H.iter_elements(H.parse(out), "a")))
        self.assertEqual(sorted(anchor.attrs), ["href"], "no attribute injected")
        self.assertIn("&quot;", out, "the quote is escaped in the attribute")

    def test_rendered_comments_use_the_spec_format(self):
        cs = C.build([self._msg("m1", "Hi!", "a1", ["sec1"], created=1_600_000_000_000_000)],
                     self.tree, self.report)
        html = C.render_comments(cs, lambda *a, **k: None, "doc.html")
        self.assertIn("Imported Quip comments", html)
        self.assertIn("Alex", html)
        self.assertIn("Hi!", html)

    def test_chat_section_heading(self):
        cs = C.build([self._msg("m1", "Hello!")], self.tree, self.report)
        html = C.render_chat(cs, lambda *a, **k: None, "doc.html")
        self.assertIn("Imported document chat messages", html)


class TestModel(unittest.TestCase):
    def test_v2_bulk_nesting_is_absorbed(self):
        """regression: docs show V2 bulk flat; the API nests under `thread`."""
        self.assertEqual(unwrap({"thread": {"id": "x"}}), {"id": "x"})
        self.assertEqual(unwrap({"id": "x"}), {"id": "x"})

    def test_type_case_difference_is_normalized(self):
        """regression: V1 says "document", V2 says "DOCUMENT"."""
        t = Thread.from_raw("t1", {"thread": {"id": "t1", "type": "document"}},
                            {"thread": {"id": "t1", "type": "DOCUMENT"}})
        self.assertEqual(t.type, "document")

    def test_secret_path_falls_back_to_the_link(self):
        t = Thread.from_raw("t1", {"thread": {"id": "t1",
                                              "link": "https://x.quip.com/abc123DEF456"}}, None)
        self.assertEqual(t.secret_path, "abc123DEF456")

    def test_v2_secret_path_wins(self):
        t = Thread.from_raw("t1",
                            {"thread": {"id": "t1", "link": "https://x.quip.com/stale"}},
                            {"thread": {"id": "t1", "secret_path": "current"}})
        self.assertEqual(t.secret_path, "current")

    def test_chat_detection(self):
        chat = Thread.from_raw("t1", {"thread": {"id": "t1", "thread_class": "two_person_chat"}},
                               {"thread": {"id": "t1", "type": "CHAT"}})
        self.assertTrue(chat.is_chat)
        doc = Thread.from_raw("t2", {"thread": {"id": "t2", "thread_class": "document"}},
                              {"thread": {"id": "t2", "type": "DOCUMENT"}})
        self.assertFalse(doc.is_chat)

    def test_embedded_spreadsheet_detection(self):
        """regression: a DOCUMENT carrying spreadsheet_summary has one embedded."""
        t = Thread.from_raw("t1", {"thread": {"id": "t1", "type": "document"}},
                            {"thread": {"id": "t1", "type": "DOCUMENT",
                                        "spreadsheet_summary": {"has_formulas": True}}})
        self.assertTrue(t.has_embedded_spreadsheet)
        self.assertTrue(t.has_formulas)
        self.assertFalse(t.is_spreadsheet)

    def test_standalone_spreadsheet_is_not_embedded(self):
        t = Thread.from_raw("t1", None, {"thread": {"id": "t1", "type": "SPREADSHEET",
                                                    "spreadsheet_summary": {}}})
        self.assertTrue(t.is_spreadsheet)
        self.assertFalse(t.has_embedded_spreadsheet)


class TestRawStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RawStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_canonical_is_parameter_order_independent(self):
        a = canonical("GET", "/1/threads/", {"ids": "x", "count": 1})
        b = canonical("GET", "/1/threads/", {"count": 1, "ids": "x"})
        self.assertEqual(a, b)

    def test_endpoint_slug_collapses_ids(self):
        self.assertEqual(endpoint_slug("GET", "/2/threads/Doc9AAxmpl2/html"),
                         "GET/2_threads_ID_html")
        self.assertEqual(endpoint_slug("GET", "/1/users/current"), "GET/1_users_current")

    def test_save_and_load_round_trip(self):
        self.store.save("GET", "/1/x", {"a": 1}, 200, {"H": "v"}, b"body")
        got = self.store.load("GET", "/1/x", {"a": 1})
        self.assertIsNotNone(got)
        self.assertEqual(got.body, b"body")
        self.assertEqual(got.status, 200)
        self.assertTrue(got.from_cache)

    def test_request_sidecar_is_greppable(self):
        self.store.save("GET", "/2/threads/T1/html", {"cursor": "abc"}, 200, {}, b"{}")
        text = next(self.store.root.rglob("*.request")).read_text()
        self.assertIn("cursor = abc", text)
        self.assertIn("GET /2/threads/T1/html", text)
        self.assertIn("status = 200", text)

    def test_answers_about_a_resource_are_cached_and_served(self):
        """403 means "you may not read this" -- a fact, not a failed attempt."""
        self.store.save("GET", "/1/blob/T/B", {}, 403, {}, b'{"error":"nope"}')
        got = self.store.load("GET", "/1/blob/T/B", {})
        self.assertEqual(got.status, 403)

    def test_failures_to_reach_the_server_are_never_served(self):
        """The one that matters for a multi-day run: a dropped connection or a
        5xx is not an answer, and serving it from cache silently loses a
        document forever."""
        for status in (0, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.store.save("GET", f"/x{status}", {}, status, {}, b"")
                self.assertIsNone(self.store.load("GET", f"/x{status}", {}))

    def test_a_failure_never_displaces_a_cached_success(self):
        """Re-fetching during an outage must not archive the good response and
        leave the canonical name holding a 500."""
        self.store.save("GET", "/a", {}, 200, {}, b"the real content")
        self.store.save("GET", "/a", {}, 500, {}, b"upstream exploded")
        got = self.store.load("GET", "/a", {})
        self.assertIsNotNone(got)
        self.assertEqual(got.body, b"the real content")
        self.assertEqual(self.store.failed_attempt_count(), 1)

    def test_a_success_does_replace_an_earlier_success(self):
        self.store.save("GET", "/a", {}, 200, {}, b"old")
        self.store.save("GET", "/a", {}, 200, {}, b"new")
        self.assertEqual(self.store.load("GET", "/a", {}).body, b"new")

    def test_retry_errors_re_requests_every_non_success(self):
        """`--retry-errors` exists for after a token's access widens, so it has
        to re-request the 403s -- which is the whole point of using it."""
        retry = RawStore(self.store.root, retry_errors=True)
        retry.save("GET", "/a", {}, 500, {}, b"")
        retry.save("GET", "/b", {}, 403, {}, b"")
        retry.save("GET", "/c", {}, 200, {}, b"ok")
        self.assertIsNone(retry.load("GET", "/a", {}))
        self.assertIsNone(retry.load("GET", "/b", {}))
        self.assertIsNotNone(retry.load("GET", "/c", {}))

    def test_index_records_every_call(self):
        self.store.save("GET", "/a", {}, 200, {}, b"1")
        self.store.save("GET", "/b", {}, 200, {}, b"22")
        stats = self.store.stats()
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["bytes"], 3)


class TestBlobExtensions(unittest.TestCase):
    """regression: blob headers are useless; sniff or use the alt attribute."""

    def test_magic_bytes(self):
        self.assertEqual(sniff_extension(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20), ".png")
        self.assertEqual(sniff_extension(b"\xff\xd8\xff\xe0" + b"\x00" * 20), ".jpg")
        self.assertEqual(sniff_extension(b"%PDF-1.7"), ".pdf")

    def test_name_hint_wins(self):
        self.assertEqual(sniff_extension(b"\x89PNG\r\n\x1a\n", "diagram.PNG"), ".png")
        self.assertEqual(sniff_extension(b"anything", "report.pdf"), ".pdf")

    def test_unknown_falls_back(self):
        self.assertEqual(sniff_extension(b"\x01\x02\x03\x04"), ".bin")

    def test_office_documents_are_distinguished_from_zip(self):
        self.assertEqual(sniff_extension(b"PK\x03\x04" + b"word/document.xml"), ".docx")
        self.assertEqual(sniff_extension(b"PK\x03\x04" + b"xl/workbook.xml"), ".xlsx")


class TestLinkPass(unittest.TestCase):
    # Realistic shapes: thread ids are 11 chars, secret paths 12, both mixed case.
    THREAD_ID = "DocAAAxmpl1"
    SECRET = "Sp1xmplPath0"
    OLD_SECRET = "aB3xKp9QrS2t"
    USER_ID = "MeuAEAself0"

    def setUp(self):
        self.doc_path = f"Imported from Quip/A/{self.SECRET}-Doc.html"
        self.index = linkpass.LinkIndex({
            "threads": {self.THREAD_ID: {"path": self.doc_path, "title": "Doc"}},
            "folders": {"FLDAOAxxxxx": {"path": "Imported from Quip/A/index.html", "title": "A"}},
            "aliases": {self.THREAD_ID: self.THREAD_ID, self.SECRET: self.THREAD_ID,
                        self.OLD_SECRET: self.THREAD_ID, "FLDAOAxxxxx": "FLDAOAxxxxx"},
        })
        self.report = {"files": 0, "rewritten": 0, "unresolved": 0, "mentions": 0}

    def test_secret_path_resolves(self):
        self.assertEqual(self.index.resolve(self.SECRET)[0], self.doc_path)

    def test_stale_secret_path_still_resolves(self):
        """Secret paths can be regenerated, so old links must still work."""
        self.assertEqual(self.index.resolve(self.OLD_SECRET)[0], self.doc_path)

    def test_unknown_token_does_not_resolve(self):
        self.assertIsNone(self.index.resolve("nope"))

    def test_link_is_rewritten_to_a_relative_path(self):
        html = f'<a href="https://x.quip.com/{self.SECRET}/Some-Title">Doc</a>'
        out, changed = linkpass._rewrite_file(
            html, "Imported from Quip/A/other.html", self.index, set(), self.report)
        self.assertTrue(changed)
        self.assertIn(f'href="{self.SECRET}-Doc.html"', out)
        self.assertIn("quip-internal-link", out)

    def test_fragment_is_preserved(self):
        html = f'<a href="https://x.quip.com/{self.SECRET}/T#section-9">x</a>'
        out, _ = linkpass._rewrite_file(
            html, "Imported from Quip/A/other.html", self.index, set(), self.report)
        self.assertIn("#section-9", out)

    def test_person_mention_becomes_plain_text(self):
        html = f'<a href="https://x.quip.com/{self.USER_ID}">@Alex</a>'
        out, _ = linkpass._rewrite_file(html, "a.html", self.index,
                                        {self.USER_ID}, self.report)
        self.assertNotIn("<a", out)
        self.assertIn("Alex", out)
        self.assertNotIn("@Alex", out)   # the @ is dropped, the name kept

    def test_unresolvable_link_is_marked_not_broken(self):
        html = '<a href="https://x.quip.com/UNKNOWNab1234">gone</a>'
        out, _ = linkpass._rewrite_file(html, "a.html", self.index, set(), self.report)
        self.assertIn("quip-unresolved", out)
        self.assertIn("https://x.quip.com/UNKNOWNab1234", out)

    def test_external_links_are_untouched(self):
        html = '<a href="https://example.com/page">ext</a>'
        out, changed = linkpass._rewrite_file(html, "a.html", self.index, set(), self.report)
        self.assertFalse(changed)
        self.assertEqual(out, html)

    def test_id_shape_recognises_real_identifiers(self):
        self.assertTrue(linkpass.looks_like_id("Doc9AAxmpl2"))    # thread id, 11 chars
        self.assertTrue(linkpass.looks_like_id("Sp1xmplPath0"))   # secret path, 12 chars
        self.assertTrue(linkpass.looks_like_id("MeuAEAself0"))    # user id

    def test_id_shape_rejects_marketing_paths(self):
        """quip.com/about is a web page, not a broken document reference."""
        for word in ("about", "chat", "pricing", "folder", "landing", "content"):
            self.assertFalse(linkpass.looks_like_id(word), word)

    def test_marketing_links_are_left_alone(self):
        html = '<a href="https://quip.com/about">About Quip</a>'
        out, changed = linkpass._rewrite_file(html, "a.html", self.index, set(), self.report)
        self.assertFalse(changed)
        self.assertNotIn("quip-unresolved", out)
        self.assertEqual(self.report["external"], 1)


class TestControls(unittest.TestCase):
    """`<control>` is Quip's generic widget wrapper, not one single thing.

    Measured: 6 of 126 are empty (file attachments); the other 120 carry real
    text -- dates, person mentions, template fields. Treating them all as
    attachments destroyed content, so these are regression tests.
    """

    def setUp(self):
        self.report = RenderReport("t", "T")

    def test_empty_control_becomes_an_attachment_placeholder(self):
        tree = H.parse("<p>before<control data-remapped='true' id='c1'></control>after</p>")
        attachments, dates = T.render_controls(tree, self.report)
        out = H.serialize(tree)
        self.assertEqual((attachments, dates), (1, 0))
        self.assertIn("quip-missing-attachment", out)
        self.assertIn('data-quip-control-id="c1"', out)
        self.assertNotIn("<control", out)
        self.assertTrue(self.report.has("unexported_attachment"))

    def test_control_with_text_is_never_replaced(self):
        tree = H.parse("<p><control id='c1'>Sam</control></p>")
        attachments, _ = T.render_controls(tree, self.report)
        out = H.serialize(tree)
        self.assertEqual(attachments, 0)
        self.assertIn("Sam", out)
        self.assertNotIn("quip-missing-attachment", out)
        self.assertFalse(self.report.has("unexported_attachment"))

    def test_template_field_control_survives(self):
        tree = H.parse("<p><control id='c1'>[Account &gt; Account Name]</control></p>")
        T.render_controls(tree, self.report)
        self.assertIn("Account Name", H.serialize(tree))

    def test_surrounding_content_is_untouched(self):
        tree = H.parse("<p>before<control id='c1'></control>after</p>")
        T.render_controls(tree, self.report)
        text = tree.text()
        self.assertIn("before", text)
        self.assertIn("after", text)

    def test_documents_without_controls_are_not_flagged(self):
        tree = H.parse("<p>ordinary</p>")
        self.assertEqual(T.render_controls(tree, self.report), (0, 0))
        self.assertFalse(self.report.has("unexported_attachment"))


class TestDateYearInference(unittest.TestCase):
    """Quip omits the year on inline date mentions; the weekday recovers it."""

    ANCHOR_2022 = int(dt.datetime(2022, 3, 1, tzinfo=dt.timezone.utc).timestamp() * 1_000_000)

    def setUp(self):
        self.report = RenderReport("t", "T")

    def _render(self, text, anchor=None):
        tree = H.parse(f"<h2><control id='c1'>{text}</control></h2>")
        if anchor is None:
            anchor = self.ANCHOR_2022
        _, dates = T.render_controls(tree, self.report, anchor)
        return H.serialize(tree), dates

    def test_year_is_recovered_from_the_weekday(self):
        out, dates = self._render("Monday, February 14")   # Feb 14 2022 was a Monday
        self.assertEqual(dates, 1)
        self.assertIn("Monday, February 14, 2022", out)

    def test_inference_is_labelled_not_passed_off_as_data(self):
        out, _ = self._render("Monday, February 14")
        self.assertIn('title="Year inferred from the weekday"', out)
        self.assertIn('data-quip-inferred-year="2022"', out)

    def test_a_nearby_year_is_used_when_the_document_year_does_not_match(self):
        """Feb 14 2020 was a Friday: the weekday points at a different year."""
        out, dates = self._render("Friday, February 14")
        self.assertEqual(dates, 1)
        self.assertIn("Friday, February 14, 2020", out)

    def test_weekday_matching_no_nearby_year_is_left_alone(self):
        """14 February falls on no Saturday between 2019 and 2025."""
        out, dates = self._render("Saturday, February 14")
        self.assertEqual(dates, 0)
        self.assertNotIn(", 20", out)

    def test_date_that_already_has_a_year_is_untouched(self):
        out, dates = self._render("Monday, September 16, 2019")
        self.assertEqual(dates, 0)
        self.assertEqual(out.count("2019"), 1)

    def test_non_date_control_text_is_untouched(self):
        out, dates = self._render("insert")
        self.assertEqual(dates, 0)
        self.assertIn("insert", out)

    def test_no_anchor_means_no_guessing(self):
        out, dates = self._render("Monday, February 14", anchor=0)
        self.assertEqual(dates, 0)
        self.assertNotIn("2022", out)


class TestMentionsInMessages(unittest.TestCase):
    """Quip stores an @mention as a bare quip.com/<user_id> URL in message text."""

    @staticmethod
    def msg(text, mention_ids=()):
        return Message.from_raw({"id": "m1", "text": text, "created_usec": 1,
                                 "author_name": "Alex", "visible": True,
                                 "mention_user_ids": list(mention_ids)})

    def test_mention_becomes_a_bold_name(self):
        m = self.msg("https://quip.com/MeuAEAself0 Does she have a burner email?",
                     ["MeuAEAself0"])
        out = C._message_html(m, "doc.html", lambda *a, **k: None,
                              lambda uid: "Alex")
        self.assertIn('<strong class="quip-mention-person">Alex</strong>', out)
        self.assertNotIn("quip.com/MeuAEAself0", out)

    def test_mention_resolves_without_mention_user_ids(self):
        """The name index catches mentions the message does not list."""
        m = self.msg("hello https://quip.com/UsrAEAxmpl1")
        out = C._message_html(m, "doc.html", lambda *a, **k: None,
                              lambda uid: "Amy" if uid == "UsrAEAxmpl1" else None)
        self.assertIn("Amy", out)
        self.assertNotIn("<a href", out)

    def test_unknown_mention_falls_back_to_a_label(self):
        m = self.msg("https://quip.com/ABCAEAxyz12", ["ABCAEAxyz12"])
        out = C._message_html(m, "doc.html", lambda *a, **k: None, lambda uid: None)
        self.assertIn("Unknown person", out)

    def test_document_links_in_messages_stay_links(self):
        m = self.msg("see https://quip.com/Sp1xmplPath0 for details")
        out = C._message_html(m, "doc.html", lambda *a, **k: None, lambda uid: None)
        self.assertIn('<a href="https://quip.com/Sp1xmplPath0"', out)

    def test_trailing_punctuation_is_not_swallowed(self):
        m = self.msg("ask https://quip.com/MeuAEAself0.", ["MeuAEAself0"])
        out = C._message_html(m, "doc.html", lambda *a, **k: None, lambda uid: "Alex")
        self.assertIn("Alex</strong>.", out)


class TestSourceLinkPreserved(unittest.TestCase):
    """The metadata 'Imported from' link must survive the link pass."""

    def test_source_link_is_not_rewritten(self):
        index = linkpass.LinkIndex({
            "threads": {"DocAAAxmpl1": {"path": "a/b.html", "title": "Doc"}},
            "folders": {}, "aliases": {"Sp1xmplPath0": "DocAAAxmpl1"}})
        stats = {"files": 0, "rewritten": 0, "unresolved": 0, "mentions": 0}
        html = ('<a href="https://x.quip.com/Sp1xmplPath0" class="quip-source-link">src</a>'
                '<a href="https://x.quip.com/Sp1xmplPath0">body link</a>')
        out, _ = linkpass._rewrite_file(html, "a/other.html", index, set(), stats)
        self.assertIn('href="https://x.quip.com/Sp1xmplPath0" class="quip-source-link"', out)
        self.assertEqual(stats["preserved"], 1)
        self.assertEqual(stats["rewritten"], 1)   # the body link still got rewritten


class TestKnownUserIds(unittest.TestCase):
    """regression: an unknown id comes back as a dict, not a string.

    The docs say `/1/users/?ids=` returns "an error message" for an id it does
    not recognise. It actually returns `{"error": "Invalid user ID."}`, so
    accepting any dict as a user mistook folder and document ids for people and
    the link pass turned real document links into plain text.
    """

    def setUp(self):
        from quiport.cli import _known_user_ids
        self._known = _known_user_ids
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RawStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_error_dicts_are_not_treated_as_people(self):
        body = json.dumps({
            "MeuAEAself0": {"name": "Alex", "id": "MeuAEAself0"},
            "BadAOAxmpl1": {"error": "Invalid user ID."},
            "BadxmplPath1": {"error": "Invalid user ID."},
        }).encode()
        self.store.save("GET", "/1/users/", {"ids": "a,b,c"}, 200, {}, body)
        self.assertEqual(self._known(self.store), {"MeuAEAself0"})

    def test_entries_without_a_name_are_ignored(self):
        body = json.dumps({"XXXAEAxxxxx": {"id": "XXXAEAxxxxx"}}).encode()
        self.store.save("GET", "/1/users/", {"ids": "x"}, 200, {}, body)
        self.assertEqual(self._known(self.store), set())


class TestExportTime(unittest.TestCase):
    def test_formats_as_local_date_and_time(self):
        from quiport.model import format_export_time
        out = format_export_time("2026-08-16T22:17:43+00:00")
        self.assertRegex(out, r"^[A-Z][a-z]{2} \d{1,2}, 2026 at \d{1,2}:\d{2} [ap]m$")

    def test_empty_input_is_empty(self):
        from quiport.model import format_export_time
        self.assertEqual(format_export_time(""), "")
        self.assertEqual(format_export_time("not a date"), "")


class TestQuipIdTypes(unittest.TestCase):
    """Character 4 of an 11-char id encodes its entity type.

    Verified against 498 ids of independently known type in the test account
    (325 users, 136 threads, 35 folders, 2 companies) with no overlap.
    """

    def test_known_types(self):
        from quiport.quipid import id_type
        self.assertEqual(id_type("MeuAEAself0"), "user")
        self.assertEqual(id_type("Usr9EAxmpl4"), "user")      # index 3 varies
        self.assertEqual(id_type("Doc9AAxmpl2"), "thread")
        self.assertEqual(id_type("DocAAAxmpl1"), "thread")
        self.assertEqual(id_type("FldAOAxmpl1"), "folder")
        self.assertEqual(id_type("Fld9OAxmpl3"), "folder")
        self.assertEqual(id_type("CoyAcAxmpl1"), "company")

    def test_secret_paths_are_not_classified(self):
        """12-char secret paths are a different namespace with no type char."""
        from quiport.quipid import id_type, is_secret_path
        self.assertIsNone(id_type("Sp1xmplPath0"))
        self.assertIsNone(id_type("Sp2xmplPath0"))
        self.assertTrue(is_secret_path("Sp1xmplPath0"))

    def test_unknown_type_char_returns_none_not_a_guess(self):
        from quiport.quipid import id_type
        self.assertIsNone(id_type("DcmABAxmpl1"))            # document_id namespace
        self.assertIsNone(id_type("short"))
        self.assertIsNone(id_type(""))

    def test_is_user_id(self):
        from quiport.quipid import is_user_id
        self.assertTrue(is_user_id("UsrAEAxmpl3"))           # "Jordan"
        self.assertTrue(is_user_id("UsrAEAxmpl2"))           # "Taylor"
        self.assertFalse(is_user_id("DocAAAxmpl1"))
        self.assertFalse(is_user_id("FldAOAxmpl1"))


class TestMentionByIdShape(unittest.TestCase):
    """A deactivated user is rejected by /1/users/, but the id still says user."""

    def setUp(self):
        self.index = linkpass.LinkIndex({"threads": {}, "folders": {}, "aliases": {}})
        self.stats = {"files": 0, "rewritten": 0, "unresolved": 0, "mentions": 0}

    def test_unresolvable_user_still_becomes_a_bold_name(self):
        html = '<p><a href="https://x.quip.com/UsrAEAxmpl3">Jordan</a></p>'
        out, changed = linkpass._rewrite_file(html, "a.html", self.index, set(), self.stats)
        self.assertTrue(changed)
        self.assertIn('<strong class="quip-mention-person">Jordan</strong>', out)
        self.assertNotIn("<a", out)
        self.assertEqual(self.stats["mentions"], 1)

    def test_thread_ids_are_not_mistaken_for_people(self):
        html = '<p><a href="https://x.quip.com/DocAAAxmpl1">A doc</a></p>'
        out, _ = linkpass._rewrite_file(html, "a.html", self.index, set(), self.stats)
        self.assertEqual(self.stats["mentions"], 0)
        self.assertIn("quip-unresolved", out)

    def test_folder_ids_are_not_mistaken_for_people(self):
        html = '<p><a href="https://x.quip.com/FldAOAxmpl1">A folder</a></p>'
        linkpass._rewrite_file(html, "a.html", self.index, set(), self.stats)
        self.assertEqual(self.stats["mentions"], 0)


class TestIconSprite(unittest.TestCase):
    """Icons are inlined as a <symbol> sprite.

    regression: the source SVGs came from Quip's live DOM and carry no
    `xmlns`, so a browser loading them through `<img src>` parses them as
    standalone XML, fails, and renders nothing.
    """

    def test_source_svgs_lack_xmlns(self):
        """The reason for inlining. If this ever changes, <img> becomes viable."""
        icons = Path(__file__).resolve().parent.parent / "quiport" / "assets" / "icons"
        for svg in icons.glob("*.svg"):
            self.assertNotIn("xmlns", svg.read_text(), f"{svg.name} now declares xmlns")

    def test_sprite_declares_xmlns_and_defines_every_kind(self):
        from quiport.render.templates import ICON_BY_KIND, icon_sprite
        sprite = icon_sprite()
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', sprite)
        for kind in ICON_BY_KIND:
            self.assertIn(f'id="quip-icon-{kind}"', sprite)

    def test_sprite_preserves_viewbox_and_paths(self):
        from quiport.render.templates import icon_sprite
        sprite = icon_sprite()
        self.assertIn('viewBox="0 0 20 20"', sprite)
        self.assertIn("<path", sprite)

    def test_icon_use_references_the_sprite(self):
        from quiport.render.templates import icon_use
        self.assertIn('href="#quip-icon-spreadsheet"', icon_use("spreadsheet"))

    def test_unknown_kind_falls_back_to_document(self):
        from quiport.render.templates import icon_use
        self.assertIn('href="#quip-icon-document"', icon_use("mystery"))


class TestEntryRow(unittest.TestCase):
    def test_row_has_icon_title_and_relative_date_slot(self):
        from quiport.reports import entry_row
        out = entry_row("My Doc", "a/b.html", "spreadsheet", 1556144668972016)
        self.assertIn('href="#quip-icon-spreadsheet"', out)
        self.assertIn(">My Doc<", out)
        self.assertIn('data-quip-updated="1556144668972016"', out)

    def test_row_needs_no_path_prefix(self):
        """Inlined icons mean a row is depth-independent."""
        from quiport.reports import entry_row
        deep = entry_row("X", "../../y.html", "chat", 1)
        self.assertIn('href="#quip-icon-chat"', deep)
        self.assertNotIn("icons/", deep)

    def test_unknown_kind_falls_back_to_the_document_icon(self):
        from quiport.reports import entry_row
        self.assertIn("#quip-icon-document", entry_row("X", "y.html", "mystery", 0))

    def test_missing_timestamp_emits_no_date_slot(self):
        from quiport.reports import entry_row
        self.assertNotIn("data-quip-updated", entry_row("X", "y.html", "document", 0))

    @staticmethod
    def _row_classes(row):
        """The <a>'s own classes -- not the icon's, which now also start
        `quip-folder-` for the colour."""
        import re
        return re.search(r'<a class="([^"]*)"', row).group(1).split()

    def test_folder_rows_are_classed_for_weighting(self):
        """Folder titles are slightly bold, so folder rows must say they are folders."""
        from quiport.reports import entry_row
        self.assertIn("quip-folder",
                      self._row_classes(entry_row("F", "f/index.html", "folder", 0)))
        self.assertNotIn("quip-folder",
                         self._row_classes(entry_row("D", "d.html", "document", 0)))

    def test_folder_class_is_not_duplicated(self):
        from quiport.reports import entry_row
        row = entry_row("F", "f/index.html", "folder", 0, extra_class="quip-folder")
        self.assertEqual(self._row_classes(row).count("quip-folder"), 1)

    def test_the_stylesheet_contains_no_control_characters(self):
        """regression: `content: "\\00b7"` in a non-raw Python string is read by
        Python first, so the CSS received a NUL byte followed by "b7" -- which
        the browser drew as a replacement glyph and stray letters."""
        from quiport.render.templates import STYLESHEET
        bad = [(i, repr(c)) for i, c in enumerate(STYLESHEET)
               if ord(c) < 32 and c not in "\n\t"]
        self.assertEqual(bad, [])

    def test_comment_markers_are_not_blue(self):
        """They annotate someone else's text; blue reads as navigation."""
        from quiport.render.templates import STYLESHEET
        for line in STYLESHEET.splitlines():
            if any(k in line for k in ("quip-comment-quote", "quip-comment-bubble",
                                       "quip-comment-gutter", "quip-comment-ref")):
                self.assertNotIn("--accent", line, line.strip())

    def test_stylesheet_weights_folder_titles(self):
        from quiport.render.templates import STYLESHEET
        self.assertIn(".quip-folder .quip-entry-title { font-weight: 600; }", STYLESHEET)


class TestReadableCellText(unittest.TestCase):
    """Quip flips cell text to white once the fill is too dark for black.

    The export carries the fill as an inline style but not that decision, so a
    dark navy cell arrives as near-black text on near-black ground.
    """

    def test_luminance_matches_wcag_reference_values(self):
        from quiport.render.transform import relative_luminance as L
        self.assertAlmostEqual(L("#000000"), 0.0, places=6)
        self.assertAlmostEqual(L("#ffffff"), 1.0, places=6)
        self.assertAlmostEqual(L("#808080"), 0.2158, places=3)
        self.assertAlmostEqual(L("#ff0000"), 0.2126, places=4)
        self.assertAlmostEqual(L("#00ff00"), 0.7152, places=4)
        self.assertAlmostEqual(L("#0000ff"), 0.0722, places=4)

    def test_shorthand_hex_is_understood(self):
        from quiport.render.transform import relative_luminance as L
        self.assertAlmostEqual(L("#fff"), L("#ffffff"), places=9)
        self.assertAlmostEqual(L("#036"), L("#003366"), places=9)

    def test_unparseable_colour_is_none_not_a_guess(self):
        from quiport.render.transform import relative_luminance as L
        for bad in ("", "red", "rgb(0,0,0)", "#12", "not a colour"):
            self.assertIsNone(L(bad), bad)

    def test_the_threshold_is_the_wcag_crossover(self):
        """Where contrast against white and against black are equal: 4.58:1."""
        from quiport.render.transform import LIGHT_TEXT_LUMINANCE as T
        white = 1.05 / (T + 0.05)
        black = (T + 0.05) / 0.05
        self.assertAlmostEqual(white, black, places=3)
        self.assertAlmostEqual(white, 4.583, places=2)

    def test_dark_fills_switch_and_light_ones_do_not(self):
        from quiport.render.transform import wants_light_text as w
        for dark in ("#032e61", "#083F91", "#000000", "#333333"):
            self.assertTrue(w(f"background-color:{dark}"), dark)
        for light in ("#ffffff", "#f0f0f0", "#FFE789", "#ff3633", "#B3E5FC"):
            self.assertFalse(w(f"background-color:{light}"), light)

    def test_black_wins_at_the_boundary(self):
        """'Black is preferred when we are on the edge' -- the comparison is
        strict, so a colour sitting exactly on the crossover keeps black."""
        from quiport.render.transform import relative_luminance, wants_light_text
        # #767676 sits just above the crossover; #757575 just below.
        self.assertGreater(relative_luminance("#767676"), 0.17913)
        self.assertFalse(wants_light_text("background-color:#767676"))
        self.assertLess(relative_luminance("#757575"), 0.17913)
        self.assertTrue(wants_light_text("background-color:#757575"))

    def test_an_explicit_text_colour_is_left_alone(self):
        """The author already chose; Quip's live-app rows set both."""
        from quiport.render.transform import wants_light_text as w
        self.assertFalse(w("color: #f26c0d; background-color: #032e61"))
        self.assertFalse(w("background-color:#032e61;color:#000"))

    def test_a_cell_with_no_background_is_untouched(self):
        from quiport.render.transform import wants_light_text as w
        self.assertFalse(w(""))
        self.assertFalse(w("width: 6em"))

    def test_only_cells_are_marked(self):
        from quiport.render import transform as T
        tree = H.parse(
            "<table><tr style='background-color:#032e61'>"
            "<td style='background-color:#032e61'>dark</td>"
            "<th style='background-color:#083F91'>head</th>"
            "<td style='background-color:#ffffff'>light</td></tr></table>")
        self.assertEqual(T.apply_readable_cell_text(tree), 2)
        out = H.serialize(tree)
        self.assertEqual(out.count("quip-on-dark"), 2)
        self.assertNotIn('<tr style=\'background-color:#032e61\' class', out)

    def test_the_stylesheet_carries_the_rule(self):
        from quiport.render.templates import STYLESHEET
        self.assertIn(".quip-body td.quip-on-dark", STYLESHEET)
        self.assertIn("color: #fff", STYLESHEET)


class TestCodeHighlighting(unittest.TestCase):
    """Highlighting is browser-side and vendored, so the archive stays
    self-contained and the stored HTML stays exactly as Quip sent it."""

    def test_the_library_is_vendored_not_linked(self):
        """A CDN reference would rot, and the archive must work from file://."""
        from quiport.render.templates import HIGHLIGHT_NAME
        path = Path(__file__).resolve().parent.parent / "quiport/assets" / HIGHLIGHT_NAME
        self.assertTrue(path.exists(), "highlight.js must be committed")
        head = path.read_text(encoding="utf-8", errors="replace")[:1200]
        self.assertIn("Highlight.js", head)
        # BSD-3-Clause requires the notice to travel with the code.
        self.assertIn("BSD-3-Clause", head, "the licence notice must survive")

    def test_document_pages_load_both_scripts_by_relative_path(self):
        from quiport.render.document import _scripts
        deep = _scripts("Imported from Quip/A/B/C/doc.html")
        self.assertIn('src="../../../../highlight.min.js"', deep)
        self.assertIn('src="../../../../quip-utils.js"', deep)
        self.assertIn("defer", deep)
        root = _scripts("doc.html")
        self.assertIn('src="highlight.min.js"', root)

    def test_only_blocks_quip_marked_as_code_are_targeted(self):
        """106 of 108 <pre> elements are live-app dumps, one of them 1.26 MB."""
        js = Path(__file__).resolve().parent.parent / "quiport/assets/quip-utils.js"
        source = js.read_text(encoding="utf-8")
        self.assertIn("pre.prettyprint", source)
        self.assertIn("MAX_HIGHLIGHT_BYTES", source)

    def test_every_language_quip_supports_is_available(self):
        """Quip's own code-block language list, so a block written there can be
        recognised as what it was."""
        import subprocess
        hljs = Path(__file__).resolve().parent.parent / "quiport/assets/highlight.min.js"
        needed = ["c", "cpp", "coffeescript", "csharp", "css", "go", "xml", "java",
                  "javascript", "json", "kotlin", "lisp", "perl", "protobuf",
                  "python", "ruby", "rust", "bash", "sql", "swift", "typescript",
                  "yaml"]
        out = subprocess.run(
            ["node", "-e", f"const h=require({str(hljs)!r});"
                           f"const need={json.dumps(needed)};"
                           "const have=new Set(h.listLanguages());"
                           "console.log(need.filter(l=>!have.has(l)).join(',')) "],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "", "missing grammars")

    def test_the_menu_offers_the_same_languages_plus_plain_text(self):
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        for label in ("CoffeeScript", "Protocol Buffer", "Kotlin", "LISP",
                      "Rust", "Swift", "Plain text"):
            self.assertIn(f"'{label}'", js, label)

    def test_switching_language_keeps_the_original_text(self):
        """The switcher re-renders from the captured source, so flipping between
        languages -- including back to plain text -- is lossless."""
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("asLines(escapeHtml(code))", js,
                      "plain text must come from the captured source")
        self.assertIn("ignoreIllegals: true", js)

    def test_pre_never_uses_a_whitespace_mode_that_eats_newlines(self):
        """regression: `white-space: nowrap`, copied from Quip's own stylesheet,
        collapsed the newlines highlight.js emits and ran an entire program onto
        one line. Harmless for Quip because it stores breaks as <br> and never
        emits a literal newline; not harmless once anything sets real text."""
        from quiport.render.templates import STYLESHEET
        # Anchored at a line start: ".quip-body pre {" is also a substring of
        # ".quip-body code, .quip-body pre {".
        rule = STYLESHEET[STYLESHEET.index("\n.quip-body pre {"):]
        rule = rule[:rule.index("}")]
        self.assertIn("white-space: pre-wrap", rule)
        self.assertNotIn("nowrap", rule)

    def test_highlighted_output_carries_breaks_as_br_like_quip_does(self):
        """Belt and braces: line structure must not rest on a CSS property."""
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("function asLines(html)", js)
        self.assertIn("replace(/\\n/g, '<br>')", js)
        # every path that writes into the block must go through it
        for path in ("asLines(result.value)", "asLines(escapeHtml(code))",
                     "asLines(hljs.highlight("):
            self.assertIn(path, js, path)

    def test_as_lines_round_trips_through_node(self):
        import subprocess
        js = Path(__file__).resolve().parent.parent / "quiport/assets/quip-utils.js"
        out = subprocess.run(["node", "-e", f"""
            global.window = global;
            global.document = {{ readyState:'complete', addEventListener(){{}},
                                 querySelectorAll(){{ return []; }} }};
            require({str(js)!r});
            console.log(JSON.stringify(window.QuipUtils.asLines('a\\nb\\n\\nc')));
        """], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '"a<br>b<br><br>c"')

    def test_auto_is_the_first_option_and_the_default(self):
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("['auto', 'Auto']", js)
        self.assertIn("select.value = 'auto';", js)
        # ...and is a menu choice, never a grammar to detect against
        self.assertIn("id !== 'plaintext' && id !== 'auto'", js)

    def test_the_control_sits_above_the_block_not_over_it(self):
        """It covered the first line of code, which is the line you most want."""
        from quiport.render.templates import STYLESHEET
        bar = STYLESHEET[STYLESHEET.index("\n.quip-code-bar {"):]
        bar = bar[:bar.index("}")]
        self.assertNotIn("position: absolute", bar)
        self.assertIn("justify-content: flex-end", bar)
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("wrap.insertBefore(bar, pre)", js, "bar must precede the pre")

    def test_the_code_icon_is_available_where_it_is_used(self):
        from quiport.render.templates import icon_sprite
        sprite = icon_sprite("code")
        self.assertIn('id="quip-icon-code"', sprite)
        self.assertIn('viewBox="0 0 16 16"', sprite)

    def test_pages_only_carry_the_symbols_they_reference(self):
        """`verify`'s icon check fails on a dangling <use>, so the emit
        condition and the JS have to agree."""
        js = (Path(__file__).resolve().parent.parent
              / "quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        doc = (Path(__file__).resolve().parent.parent
               / "quiport/render/document.py").read_text(encoding="utf-8")
        self.assertIn("#quip-icon-code", js)
        self.assertIn('"prettyprint" in (thread.html or "")', doc)

    def test_the_theme_is_in_the_archive_stylesheet(self):
        from quiport.render.templates import STYLESHEET
        self.assertIn(".hljs-comment", STYLESHEET)
        self.assertIn(".quip-body pre.quip-highlighted", STYLESHEET)


class TestFactcheckNeverPassesVacuously(unittest.TestCase):
    """It is the gate before a multi-hour run, so a wrong green light is the
    worst outcome. A call that never answered must not become an empty result."""

    def _resp(self, status, body=b"{}"):
        from quiport.quipclient import Response
        return Response(status, {}, body, "/x", {})

    def test_a_failure_to_answer_raises(self):
        from quiport.factcheck import ProbeFailed, _answered
        for status in (0, 500, 502, 503, 429):
            with self.subTest(status=status):
                with self.assertRaises(ProbeFailed):
                    _answered(self._resp(status), "probe")

    def test_a_real_refusal_is_information_not_a_failure(self):
        from quiport.factcheck import _answered
        self.assertIsNone(_answered(self._resp(403), "probe"))

    def test_unreadable_json_raises(self):
        from quiport.factcheck import ProbeFailed, _answered
        with self.assertRaises(ProbeFailed):
            _answered(self._resp(200, b"not json"), "probe")

    def test_a_good_response_is_parsed(self):
        from quiport.factcheck import _answered
        self.assertEqual(_answered(self._resp(200, b'{"a":1}'), "probe"), {"a": 1})

    def test_a_probe_failure_is_recorded_as_FAIL_not_swallowed(self):
        """regression: `q.folders(...) or {}` meant a failed sample produced
        "0 threads had a problem", which reads exactly like a pass."""
        from quiport import factcheck as F

        class Client:
            stats = {"network": 0}
            def get(self, endpoint, **params):
                if endpoint == "/1/users/current":
                    from quiport.quipclient import Response
                    return Response(200, {}, json.dumps(
                        {"id": "U1", "name": "T", "url": "https://x.quip.com",
                         "private_folder_id": "F1"}).encode(), endpoint, params)
                from quiport.quipclient import Response
                return Response(500, {}, b'{"error":"boom"}', endpoint, params)
            def get_json(self, endpoint, **params):
                r = self.get(endpoint, **params)
                return r.json() if r.ok else None

        fc = F.FactCheck(Client())
        checks = fc.run()
        statuses = {c.name: c.status for c in checks}
        self.assertEqual(statuses.get("discovery"), F.FAIL)
        self.assertTrue(any(c.status == F.FAIL for c in checks))

    def test_factcheck_exits_non_zero_when_a_probe_fails(self):
        from quiport import factcheck as F
        checks = [F.Check("discovery", F.FAIL, "could not sample the account")]
        self.assertTrue(any(c.status == F.FAIL for c in checks))


class TestCallSitesAgreeWithHelpers(unittest.TestCase):
    """Every module must unpack the endpoint helpers the way they return.

    `thread_html_v2` grew a third value (`complete`) and `factcheck` still
    unpacked two — which only surfaced part-way through a live run against a
    real account, because nothing else exercises that module.
    """

    def test_every_module_imports(self):
        import importlib, pkgutil
        import quiport
        failures = []
        for mod in pkgutil.walk_packages(quiport.__path__, "quiport."):
            if mod.name.endswith("__main__"):
                continue          # an entry point: importing it runs the CLI
            try:
                importlib.import_module(mod.name)
            except Exception as e:                       # noqa: BLE001
                failures.append(f"{mod.name}: {e}")
        self.assertEqual(failures, [])

    def test_thread_html_v2_returns_three_values(self):
        from quiport import quipclient as q

        class Client:
            def paginate_all(self, endpoint, **kw):
                return [{"html": "<p>x</p>", "response_metadata": {"next_cursor": ""}}], True

        html, pages, complete = q.thread_html_v2(Client(), "T1")
        self.assertEqual((html, pages, complete), ("<p>x</p>", 1, True))

    def test_no_call_site_unpacks_it_wrongly(self):
        """Cheap structural check across the whole package."""
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "quiport"
        wrong = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                call = node.value
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "thread_html_v2"):
                    continue
                target = node.targets[0]
                n = len(target.elts) if isinstance(target, ast.Tuple) else 1
                if n != 3:
                    wrong.append(f"{path.name}:{node.lineno} unpacks {n}")
        self.assertEqual(wrong, [])

    def test_the_unsafe_paginate_generator_is_gone(self):
        """It could not distinguish a finished chain from a broken one."""
        from quiport.quipclient import QuipClient
        self.assertFalse(hasattr(QuipClient, "paginate"))
        self.assertTrue(hasattr(QuipClient, "paginate_all"))


class TestSiteChrome(unittest.TestCase):
    """Nav and search on every page, from one shared index."""

    def test_nav_paths_climb_out_of_nested_folders(self):
        from quiport.render.templates import site_chrome
        deep = site_chrome("Imported from Quip/A/B/Doc.html")
        self.assertIn('href="../../../index.html"', deep)
        top = site_chrome("index.html")
        self.assertIn('href="index.html"', top)

    def test_the_current_page_is_marked(self):
        from quiport.render.templates import site_chrome
        html = site_chrome("recent.html", "recent.html")
        self.assertIn('class="quip-nav-current" href="recent.html"', html)

    def test_chrome_sits_outside_the_compared_body(self):
        """`verify`'s content check reads `.quip-body` and compares it word for
        word against the raw API text. Nav words inside it would look like text
        that appeared from nowhere."""
        from quiport.render import document
        import inspect
        src = inspect.getsource(document.render_document)
        self.assertLess(src.index("site_chrome(page_path)"),
                        src.index('article class="quip-body"'))

    def test_the_index_is_one_shared_file_not_a_copy_per_page(self):
        from quiport import reports
        import inspect
        self.assertIn("SEARCH_INDEX_NAME", inspect.getsource(reports.write_all))
        # ...and index() no longer inlines its own copy
        self.assertNotIn("QUIP_INDEX = ", inspect.getsource(reports.index))

    def test_search_entries_carry_every_filterable_field(self):
        from quiport.reports import search_index
        from quiport.model import Thread
        t = Thread(id="T1", title="Doc", type="document")
        t.updated_usec, t.created_usec = 200, 100
        t.member_ids = ["a", "b", "c"]
        payload = search_index({"T1": t}, [])
        for key in ('"k"', '"u"', '"cr"', '"m"', '"s"'):
            self.assertIn(key, payload)
        self.assertIn('"m":3', payload)
        self.assertIn('"s":"shared"', payload)

    def test_share_buckets(self):
        from quiport.reports import _share_bucket
        from quiport.model import Thread
        solo = Thread(id="a"); solo.member_ids = ["me"]
        pair = Thread(id="b"); pair.member_ids = ["me", "you"]
        corp = Thread(id="c"); corp.sharing = {"company_mode": "VIEW"}
        corp.member_ids = ["me", "you"]
        self.assertEqual(_share_bucket(solo), "private")
        self.assertEqual(_share_bucket(pair), "shared")
        self.assertEqual(_share_bucket(corp), "company")

    def test_search_filters_run_against_the_generated_index(self):
        """Exercises the shipped JS over a real generated payload."""
        import subprocess, tempfile as tf
        from quiport.reports import search_index
        from quiport.model import Thread
        js = Path(__file__).resolve().parent.parent / "quiport/assets/quip-utils.js"

        threads = {}
        for i, (kind, members, created, company) in enumerate([
                ("document", 1, 1_000_000_000_000_000, False),
                ("document", 5, 1_600_000_000_000_000, False),
                ("spreadsheet", 2, 1_600_000_000_000_000, False),
                ("document", 9, 1_700_000_000_000_000, True)]):
            t = Thread(id=f"T{i}", title=f"Doc {i}", type=kind)
            t.created_usec, t.updated_usec = created, created + 1
            t.member_ids = [f"u{j}" for j in range(members)]
            if company:
                t.sharing = {"company_mode": "VIEW"}
            threads[t.id] = t
        payload = search_index(threads, [])

        with tf.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(payload)
            index_path = fh.name

        out = subprocess.run(["node", "-e", f"""
            const fs = require('fs');
            global.window = global;
            global.document = {{ readyState:'complete', addEventListener(){{}},
                                 querySelectorAll(){{ return []; }},
                                 querySelector(){{ return null; }} }};
            eval(fs.readFileSync({index_path!r}, 'utf8'));
            require({str(js)!r});
            function el(a) {{ return {{ attrs:a||{{}}, hidden:false, value:'',
              innerHTML:'', style:{{}}, L:{{}}, querySelectorAll(){{ return []; }},
              addEventListener(t,f){{ (this.L[t]=this.L[t]||[]).push(f); }},
              getAttribute(n){{ return this.attrs[n]; }},
              fire(t){{ (this.L[t]||[]).forEach(function(f){{ f(); }}); }} }}; }}
            var box=el(), out=el(), nav=el({{href:'index.html'}});
            var f={{type:el(),share:el(),members:el(),created:el(),modified:el()}};
            var scope={{ querySelector:function(s){{ return {{'#quip-q':box,
              '#quip-results':out,'#quip-f-type':f.type,'#quip-f-share':f.share,
              '#quip-f-members':f.members,'#quip-f-created':f.created,
              '#quip-f-modified':f.modified,'.quip-nav a':nav}}[s] || null; }},
              querySelectorAll:function(){{ return []; }} }};
            window.QuipUtils.initSiteSearch(scope);
            function n() {{ var m=out.innerHTML.match(/(\\d+) match/);
                            return out.hidden ? -1 : (m ? Number(m[1]) : 0); }}
            var r = {{}};
            r.idle = n();
            f.type.value='spreadsheet'; f.type.fire('change'); r.spreadsheet = n();
            f.type.value=''; f.share.value='company'; f.share.fire('change'); r.company = n();
            f.share.value=''; f.members.value='5'; f.members.fire('change'); r.members5 = n();
            f.members.value=''; f.created.value='2020-01-01';
            f.created.fire('change'); r.created2020 = n();
            console.log(JSON.stringify(r));
        """], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip())
        self.assertEqual(got["idle"], -1, "no query and no filters shows nothing")
        self.assertEqual(got["spreadsheet"], 1)
        self.assertEqual(got["company"], 1)
        self.assertEqual(got["members5"], 2, "5 and 9 members")
        self.assertEqual(got["created2020"], 3, "the 2001 document drops out")


class TestReportPageScripts(unittest.TestCase):
    def test_recent_page_loads_the_script_it_depends_on(self):
        """Report pages carried no <script> at all -- only index.html did --
        so the relative dates rendered as their static fallback and the
        created-date filter never initialised. The feature was inert."""
        from quiport import reports
        import inspect
        src = inspect.getsource(reports.recent)
        self.assertIn("chrome_scripts", src)
        self.assertIn("extra_head", src)

    def test_a_page_with_live_dates_must_ship_the_script(self):
        """The pairing is the invariant: emit `data-quip-updated` and you owe
        the page the script that fills it in."""
        from quiport import reports
        import inspect
        for name in ("recent",):
            src = inspect.getsource(getattr(reports, name))
            if "data-quip-updated" in src:
                self.assertIn("chrome_scripts", src,
                              f"{name}() emits live dates but loads no script")

    def test_created_column_is_static(self):
        """It is the column the filter acts on, so it has to be readable with
        scripting off."""
        from quiport import reports
        import inspect
        src = inspect.getsource(reports.recent)
        created = src[src.index("quip-recent-created"):]
        self.assertNotIn("data-quip-when", created[:200])

    def test_short_dates_stay_narrow(self):
        import datetime as d
        from quiport.reports import _short_date
        self.assertEqual(
            _short_date(d.datetime(2016, 6, 23, tzinfo=d.timezone.utc).timestamp() * 1e6),
            "Jun 23, 2016")
        self.assertEqual(_short_date(0), "—")


class TestRecentsFilter(unittest.TestCase):
    """Filter on creation, because modification timestamps move.

    One real account had 165 documents created before 2020 whose
    `updated_usec` all changed within a single week -- a bulk event, not
    editing -- which pushed decade-old material to the top of the list.
    """

    def test_rows_carry_the_creation_stamp(self):
        from quiport import reports
        import inspect
        self.assertIn('data-quip-created=', inspect.getsource(reports.recent))

    def test_the_filter_runs_against_the_real_markup(self):
        """Exercises the shipped JS, not a description of it."""
        import subprocess
        js = Path(__file__).resolve().parent.parent / "quiport/assets/quip-utils.js"
        out = subprocess.run(["node", "-e", f"""
            global.window = global;
            global.document = {{ readyState:'complete', addEventListener(){{}},
                                 querySelectorAll(){{ return []; }},
                                 querySelector(){{ return null; }} }};
            require({str(js)!r});
            function el(a) {{ return {{ attrs:a||{{}}, hidden:false, value:'',
              textContent:'', L:{{}},
              addEventListener(t,f){{ (this.L[t]=this.L[t]||[]).push(f); }},
              getAttribute(n){{ return this.attrs[n]; }},
              fire(t){{ (this.L[t]||[]).forEach(function(f){{ f(); }}); }} }}; }}
            var usec = function (y) {{ return Date.UTC(y,0,2) * 1000; }};
            var items = [el({{'data-quip-created': usec(2014)}}),
                         el({{'data-quip-created': usec(2015)}}),
                         el({{'data-quip-created': usec(2025)}})];
            var oldGroup = el(); oldGroup.querySelectorAll = function () {{
              return items.slice(0,2).filter(function (i) {{ return !i.hidden; }}); }};
            var newGroup = el(); newGroup.querySelectorAll = function () {{
              return items.slice(2).filter(function (i) {{ return !i.hidden; }}); }};
            var input = el(), clear = el(), status = el();
            var scope = {{
              querySelector: function (s) {{ return {{'#quip-created-after': input,
                '#quip-created-clear': clear, '#quip-filter-status': status}}[s]; }},
              querySelectorAll: function (s) {{
                return s === '.quip-recent-item' ? items : [oldGroup, newGroup]; }} }};
            window.QuipUtils.initRecentFilter(scope);
            input.value = '2020-01-01'; input.fire('change');
            console.log(JSON.stringify({{
              hidden: items.map(function (i) {{ return i.hidden; }}),
              emptyGroupHidden: oldGroup.hidden, liveGroupHidden: newGroup.hidden,
              status: status.textContent }}));
            clear.fire('click');
            console.log(JSON.stringify(items.map(function (i) {{ return i.hidden; }})));
        """], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        first, second = out.stdout.strip().splitlines()
        got = json.loads(first)
        self.assertEqual(got["hidden"], [True, True, False])
        self.assertTrue(got["emptyGroupHidden"], "a day heading with nothing under it")
        self.assertFalse(got["liveGroupHidden"])
        self.assertIn("2 of 3 hidden", got["status"])
        self.assertEqual(json.loads(second), [False, False, False], "Clear restores")

    def test_the_headings_always_carry_the_year(self):
        """An archive outlives the year it was taken in."""
        import datetime as d
        from quiport.reports import _day_heading
        self.assertEqual(
            _day_heading(d.datetime(2026, 10, 17, tzinfo=d.timezone.utc).timestamp() * 1e6),
            "October 17, 2026")

    def test_column_headers_match_the_row_columns(self):
        from quiport import reports
        import inspect
        src = inspect.getsource(reports.recent)
        self.assertIn(">Members<", src)
        self.assertIn(">Updated<", src)


class TestLinkSchemes(unittest.TestCase):
    def test_non_web_schemes_are_not_treated_as_file_paths(self):
        """25 phone numbers in one real account were reported as 181 broken
        links, because the skip list named schemes one by one and `tel:` was
        not among them."""
        from quiport.verify import _URI_SCHEME
        for value in ("tel:5555550123", "mailto:a@b.c", "sms:+15551234",
                      "callto:x", "http://x", "https://x", "data:text/plain,x"):
            self.assertTrue(_URI_SCHEME.match(value), value)

    def test_real_relative_paths_are_still_checked(self):
        from quiport.verify import _URI_SCHEME
        for value in ("folder/Doc.html", "../a/b.html", "Doc-name.html",
                      "C%3A-thing.html"):
            self.assertFalse(_URI_SCHEME.match(value), value)

    def test_generated_filenames_cannot_contain_a_colon(self):
        """The scheme rule is only safe because of this."""
        from quiport.naming import slugify
        self.assertNotIn(":", slugify("Level 3 Incident: Late Deploy"))


class TestRecentsPage(unittest.TestCase):
    """Modelled on Quip's own recents page (original-references/)."""

    def _thread(self, **kw):
        from quiport.model import Thread
        t = Thread(id=kw.pop("id", "T1"))
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    def test_company_sharing_is_not_expanded_into_a_headcount(self):
        """V1's `expanded_user_ids` lists everyone who *could* reach a
        company-shared thread -- 7,127 on a real account, against 103 actual
        members. As a label that reads as though a private note were shared
        with the world."""
        from quiport.reports import sharing_label
        t = self._thread(sharing={"company_mode": "VIEW"},
                         expanded_user_ids=[f"U{i}" for i in range(7127)],
                         member_ids=[f"M{i}" for i in range(103)])
        self.assertEqual(sharing_label(t), "Company")

    def test_private_and_counted_sharing(self):
        from quiport.reports import sharing_label
        self.assertEqual(sharing_label(self._thread(member_ids=["me"])), "Private")
        self.assertEqual(sharing_label(self._thread()), "Private")
        self.assertEqual(sharing_label(self._thread(member_ids=["a", "b", "c"])),
                         "3 people")

    def test_membership_is_counted_once_per_person(self):
        from quiport.reports import sharing_label
        self.assertEqual(sharing_label(self._thread(member_ids=["a", "a", "b"])),
                         "2 people")

    def test_day_headings_always_carry_the_year(self):
        """Quip omits the year for the current one; an archive cannot, because
        it is read after the year it was taken in has passed."""
        import datetime as dt
        from quiport.reports import _day_heading
        this_year = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc).timestamp() * 1e6
        older = dt.datetime(2025, 9, 10, tzinfo=dt.timezone.utc).timestamp() * 1e6
        self.assertEqual(_day_heading(this_year), "October 17, 2026")
        self.assertEqual(_day_heading(older), "September 10, 2025")

    def test_no_relative_day_headings(self):
        """A snapshot is read long after it is written; a heading that says
        "Today" becomes a lie the next morning."""
        from quiport import reports
        import inspect
        src = inspect.getsource(reports._day_heading)
        self.assertNotIn("Today", src.split('"""')[2] if '"""' in src else src)

    def test_the_when_column_asks_for_the_recent_format(self):
        from quiport import reports
        import inspect
        self.assertIn('data-quip-when="recent"', inspect.getsource(reports.recent))

    def test_js_switches_to_an_exact_date_after_a_month(self):
        js = pathlib.Path("quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("function recentTime", js)
        self.assertIn("seconds < MONTH ? relativeTime(usec, nowMs) : shortDate(usec)", js)
        self.assertIn("data-quip-when", js)


class TestEstimate(unittest.TestCase):
    def test_it_models_the_hourly_quota_not_just_the_per_minute_one(self):
        """750/hour is 12.5/min sustained. Estimating from the 50/min pool
        alone -- or the 100 the old code assumed -- understates a long run by
        several times."""
        from quiport import cli
        self.assertEqual(cli.USER_CALLS_PER_HOUR, 750)
        self.assertEqual(cli.USER_CALLS_PER_MINUTE, 50)
        per_hour = min(cli.USER_CALLS_PER_MINUTE * 60, cli.USER_CALLS_PER_HOUR)
        self.assertEqual(per_hour, 750, "the hourly pool must bind")

    def test_the_default_cost_per_thread_includes_blobs(self):
        """Measured 7.7 with full sharing; the old estimate counted html,
        messages and sharing only, and was 35% low."""
        from quiport import cli
        self.assertGreater(cli.CALLS_PER_THREAD["full"], 5)
        self.assertLess(cli.CALLS_PER_THREAD["lean"], cli.CALLS_PER_THREAD["full"])

    def test_it_prefers_measurement_over_the_table(self):
        from quiport import cli
        import inspect
        src = inspect.getsource(cli.cmd_estimate)
        self.assertIn("measured from this export", src)
        self.assertIn("if done >= 25", src)


class TestNothingIsLostOnFailure(unittest.TestCase):
    """The two ways a call can fail must not look the same.

    A quota refusal has to be waited out forever -- giving up would lose the
    resource. A resource that is genuinely gone has to be recorded and stepped
    over -- retrying forever wedges the whole harvest on one blob.
    """

    QUOTA_SPENT = {"X-Ratelimit-Limit": "750", "X-Ratelimit-Remaining": "0",
                   "X-Company-Ratelimit-Remaining": "581"}
    QUOTA_LEFT = {"X-Ratelimit-Limit": "750", "X-Ratelimit-Remaining": "740",
                  "X-Company-Ratelimit-Remaining": "581"}

    def _fetch_503(self, headers, sleep_budget=12):
        import urllib.error, io
        from quiport import quipclient as Q

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "Unavailable",
                                         headers, io.BytesIO(b""))

        n = [0]
        def counted(_d):
            n[0] += 1
            if n[0] > sleep_budget:
                raise TimeoutError("still retrying")

        real = (Q.urllib.request.urlopen, Q.time.sleep)
        Q.urllib.request.urlopen, Q.time.sleep = boom, counted
        try:
            c = Q.QuipClient("dummy", "https://platform.quip.com")
            return c._fetch("/1/blob/T/B", {})
        finally:
            Q.urllib.request.urlopen, Q.time.sleep = real

    def test_a_quota_refusal_is_never_given_up_on(self):
        with self.assertRaises(TimeoutError):
            self._fetch_503(self.QUOTA_SPENT)

    def test_a_503_with_quota_left_is_returned_not_retried_forever(self):
        """Observed live: a blob 503ing while the pools still had requests
        left. Treated as throttling it would block the run indefinitely."""
        from quiport import quipclient as Q
        resp = self._fetch_503(self.QUOTA_LEFT)
        self.assertEqual(resp.status, 503)
        self.assertFalse(Q.is_answer(resp.status),
                         "503 must not be cached as a settled answer")

    def test_a_503_that_did_not_answer_leaves_the_thread_pending(self):
        """`Incomplete` -> ledger status 'error' -> not 'done' -> retried.

        If it were marked done the blob would be stranded forever, because a
        resumed harvest skips completed threads.
        """
        from quiport.state import Ledger
        with tempfile.TemporaryDirectory() as d:
            led = Ledger(Path(d) / "ledger.jsonl")
            led.mark("harvest", "T1", status="error", error="blob did not answer")
            led.mark("harvest", "T2", status="done")
            self.assertFalse(led.is_done("harvest", "T1"))
            self.assertTrue(led.is_done("harvest", "T2"))
            reloaded = Ledger(Path(d) / "ledger.jsonl")
            self.assertFalse(reloaded.is_done("harvest", "T1"),
                             "must survive a restart, not just this process")

    def test_blob_failures_are_distinguished_from_thread_failures(self):
        """403 on a blob is a real answer -- blob permissions are independent
        of thread permissions -- and must not fail the whole thread."""
        from quiport import harvest as H
        import inspect
        src = inspect.getsource(H._fetch_blobs)
        self.assertIn("is_answer", src)
        self.assertIn("raise Incomplete", src)


class TestFolderColors(unittest.TestCase):
    """Colours sampled from Quip's own colour picker screenshot."""

    def test_every_api_colour_has_a_rule(self):
        """These are the values `/1/folders/` actually returned on two real
        accounts, plus the two the screenshot shows but neither account used."""
        from quiport.render.templates import FOLDER_COLORS, STYLESHEET
        seen_in_the_wild = {"manila", "green", "blue", "red", "purple",
                            "light_blue", "light_green", "orange",
                            "light_purple", "light_orange"}
        for name in seen_in_the_wild | {"yellow", "light_red"}:
            self.assertIn(name, FOLDER_COLORS)
            self.assertIn(f".quip-folder-{name} ", STYLESHEET)

    def test_an_unknown_colour_degrades_to_the_default(self):
        from quiport.render.templates import icon_use
        self.assertIn("quip-folder-manila", icon_use("folder", "chartreuse"))

    def test_a_folder_with_no_colour_still_gets_a_class(self):
        """Quip's system roots -- Private, Shared, Archive, Starred -- carry no
        `color` field at all. Without a class the icon inherits `color` from the
        row's link and renders in the accent blue."""
        from quiport.render.templates import icon_use
        self.assertIn("quip-folder-manila", icon_use("folder", ""))
        self.assertIn("quip-folder-manila", icon_use("folder", None))

    def test_colour_only_applies_to_folders(self):
        from quiport.render.templates import icon_use
        self.assertNotIn("quip-folder-", icon_use("document", "blue"))

    def test_the_icon_body_takes_currentcolor(self):
        """One sprite symbol serves all twelve; a hardcoded fill would make
        every folder manila no matter what class the row carries."""
        from quiport.render.templates import icon_sprite
        sprite = icon_sprite("folder")
        self.assertIn('fill="currentColor"', sprite)
        self.assertNotIn("fill: rgb(255, 198, 77)", sprite)

    def test_js_and_python_agree_on_the_class_name(self):
        js = pathlib.Path("quiport/assets/quip-utils.js").read_text(encoding="utf-8")
        self.assertIn("quip-folder-", js)
        from quiport.render.templates import icon_use
        self.assertIn("quip-folder-blue", icon_use("folder", "blue"))

    def test_colour_is_read_from_cache_when_absent_from_state(self):
        """Exports discovered before the field existed must not need a
        re-discover to get their colours."""
        from quiport.render import _folder_colors_from_cache
        import inspect
        src = inspect.getsource(_folder_colors_from_cache)
        self.assertIn("/1/folders/?ids=", src)
        self.assertIn("iter_index", src)


class TestSharingModeIsRecorded(unittest.TestCase):
    """`audit --sharing full` over a `--sharing lean` thread reports a gap lean
    will never fill, so the audit/harvest loop spins without progress."""

    def test_harvest_records_the_mode_it_used(self):
        from quiport import harvest as H
        import inspect
        src = inspect.getsource(H.harvest)
        self.assertIn('ledger.mark("harvest", tid, status="done", sharing=cfg.sharing',
                      src)

    def test_audit_judges_each_thread_by_its_recorded_mode(self):
        from quiport import audit as A
        import types, json as J

        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / "ledger.jsonl").write_text("\n".join([
                J.dumps({"phase": "harvest", "unit": "LEAN", "sharing": "lean"}),
                J.dumps({"phase": "harvest", "unit": "FULL", "sharing": "full"}),
                J.dumps({"phase": "harvest", "unit": "OLD"}),          # pre-dates the field
                "not json",
            ]) + "\n", encoding="utf-8")
            cfg = types.SimpleNamespace(state_dir=state, sharing="full")
            modes = A._sharing_modes(cfg)

        self.assertEqual(modes.get("LEAN"), "lean")
        self.assertEqual(modes.get("FULL"), "full")
        self.assertNotIn("OLD", modes, "no record means fall back to the flag")

    def test_missing_ledger_is_not_an_error(self):
        from quiport import audit as A
        import types
        with tempfile.TemporaryDirectory() as d:
            cfg = types.SimpleNamespace(state_dir=Path(d), sharing="full")
            self.assertEqual(A._sharing_modes(cfg), {})


class TestResumeIsCheap(unittest.TestCase):
    """A resumed harvest must not re-fetch what it already has."""

    @staticmethod
    def _keys_for(pending, all_ids):
        from quiport import harvest as H

        asked = []

        class C:
            store = None
            stats = {"network": 0}

            def get_json(self, endpoint, **params):
                asked.append((endpoint, params.get("ids")))
                return {i: {"thread": {"id": i}} for i in params["ids"].split(",")}

        H._fetch_metadata(C(), all_ids, {"errors": 0}, pending=pending)
        return asked

    def test_batch_keys_do_not_move_when_pending_shrinks(self):
        """Cutting batches from the shrinking pending list realigned every
        boundary, so each resume asked for id-tuples it had never asked for
        before and none of them hit the cache. Measured on a real account:
        285 calls for 143 distinct batches, one thread described six times."""
        all_ids = [f"T{i:04d}" for i in range(200)]

        first = self._keys_for(all_ids, all_ids)
        # 93 threads finished, as after an interrupted run
        later = self._keys_for(all_ids[93:], all_ids)

        first_keys = set(first)
        for endpoint, ids in later:
            self.assertIn((endpoint, ids), first_keys,
                          "a resumed run asked for a batch the first run never did")

    def test_fully_harvested_batches_are_skipped(self):
        all_ids = [f"T{i:04d}" for i in range(200)]
        # only the last 25 remain
        asked = self._keys_for(all_ids[175:], all_ids)
        self.assertEqual(len(asked), 2, "one V1 + one V2 call for the one live window")

    def test_every_pending_thread_is_still_described(self):
        """Skipping windows must never skip a thread that needs fetching."""
        all_ids = [f"T{i:04d}" for i in range(200)]
        pending = all_ids[3::37]
        asked = self._keys_for(pending, all_ids)
        covered = {i for _, ids in asked for i in ids.split(",")}
        for t in pending:
            self.assertIn(t, covered)


class TestRateLimitRecovery(unittest.TestCase):
    """Headers as the API actually spells them, taken from a real response."""

    REAL = {"X-Ratelimit-Limit": "50", "X-Ratelimit-Remaining": "42",
            "X-Ratelimit-Reset": "1786967340", "Retry-After": "23",
            "X-Company-Ratelimit-Limit": "600",
            "X-Company-Ratelimit-Remaining": "592",
            "X-Company-Ratelimit-Reset": "1786967340",
            "X-Company-Retry-After": "23"}

    def test_company_pool_is_parsed_despite_its_spelling(self):
        """Quip sends `X-Company-Ratelimit-*` (lowercase l) while the per-user
        headers are `X-Ratelimit-*`. Matching only the exact name and its
        all-lowercase form missed the company pool completely, so the limiter
        never paced against it and learned it was exhausted by being refused."""
        from quiport.quipclient import RateLimiter
        r = RateLimiter()
        r.observe(self.REAL)
        self.assertEqual(r.remaining, 42)
        self.assertEqual(r.company_remaining, 592)
        self.assertEqual(r.company_reset_at, 1786967340)

    def test_header_lookup_is_case_insensitive_either_way(self):
        from quiport.quipclient import RateLimiter
        for spelling in ("X-Company-RateLimit-Remaining",
                         "x-company-ratelimit-remaining",
                         "X-COMPANY-RATELIMIT-REMAINING"):
            r = RateLimiter()
            r.observe({spelling: "7"})
            self.assertEqual(r.company_remaining, 7, spelling)

    def test_the_limiter_waits_on_an_exhausted_company_pool(self):
        from quiport import quipclient as Q
        r = Q.RateLimiter()
        r.observe({"X-Company-Ratelimit-Remaining": "1",
                   "X-Company-Ratelimit-Reset": str(int(time.time()) + 40)})
        slept = []
        real_sleep, Q.time.sleep = Q.time.sleep, lambda d: slept.append(d)
        try:
            r.wait_if_needed()
        finally:
            Q.time.sleep = real_sleep
        self.assertTrue(slept and slept[0] > 30,
                        "should wait out the company pool, not just the user one")

    def test_repeated_refusals_widen_the_wait(self):
        """`Retry-After` describes the window the server reports. If a longer
        pool is the real block, honouring it literally means sleeping the same
        30s forever on the same call."""
        import urllib.error, io
        from quiport import quipclient as Q

        hdrs = {"Retry-After": "23", "X-Ratelimit-Reset": str(int(time.time()) + 23),
                "X-Company-Ratelimit-Remaining": "0",
                "X-Company-Ratelimit-Reset": str(int(time.time()) + 3000)}

        def always429(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many",
                                         hdrs, io.BytesIO(b"{}"))

        slept, calls = [], []
        real_open, real_sleep = Q.urllib.request.urlopen, Q.time.sleep

        def stop_after_six(d):
            slept.append(d)
            if len(slept) > 12:
                raise KeyboardInterrupt
        Q.urllib.request.urlopen, Q.time.sleep = always429, stop_after_six
        try:
            c = Q.QuipClient("dummy", "https://platform.quip.com")
            with self.assertRaises(KeyboardInterrupt):
                c._fetch("/1/folders/", {"ids": "X"})
        finally:
            Q.urllib.request.urlopen, Q.time.sleep = real_open, real_sleep

        throttle_sleeps = [d for d in slept if d >= 23]
        self.assertGreater(len(throttle_sleeps), 3)
        self.assertGreater(throttle_sleeps[2], throttle_sleeps[0],
                           "the wait must grow, not repeat")
        self.assertLessEqual(max(throttle_sleeps), Q.MAX_THROTTLE_SLEEP)
        # Bounded so progress stays visible and Ctrl-C stays prompt, even
        # though an hour-long quota really can outlast it.
        self.assertLessEqual(Q.MAX_THROTTLE_SLEEP, 300)

    def test_the_throttle_log_names_both_pools(self):
        """Without this the operator cannot tell a 30s per-minute wait from an
        hour-long company block."""
        from quiport.quipclient import RateLimiter
        r = RateLimiter()
        r.observe(self.REAL)
        text = r.describe()
        self.assertIn("user", text)
        self.assertIn("company", text)
        self.assertIn("resets in", text)


class TestInterruptHandling(unittest.TestCase):
    def test_ctrl_c_re_raises_so_shell_loops_stop(self):
        """A shell aborts a loop only when the child *died* from SIGINT.

        Catching it and returning 130 looks like an ordinary failed command,
        so `until audit; do harvest; done` starts the next iteration and
        Ctrl-C can never escape. Verified under a real tty: with the return,
        the loop ran a second iteration; with the re-raise, it stopped at 130.
        """
        from quiport import cli
        import inspect
        src = inspect.getsource(cli.main)
        handler = src[src.index("except KeyboardInterrupt"):]
        self.assertIn("signal.SIG_DFL", handler)
        self.assertIn("os.kill(os.getpid(), signal.SIGINT)", handler)
        # the human-readable message must be printed before we die
        self.assertLess(handler.index("Interrupted."),
                        handler.index("os.kill("))

    def test_interrupt_message_says_the_work_is_kept(self):
        from quiport import cli
        import inspect
        src = inspect.getsource(cli.main)
        self.assertIn("re-run to ", src)
        self.assertIn("on disk", src)


class TestProgressReporting(unittest.TestCase):
    """Silence is indistinguishable from a hang."""

    def test_heartbeat_fires_on_a_timer(self):
        from quiport.harvest import _Heartbeat
        b = _Heartbeat(0.05)
        self.assertFalse(b.due())
        time.sleep(0.06)
        self.assertTrue(b.due())
        self.assertFalse(b.due(), "resets after firing")

    def test_thread_progress_is_time_based_not_only_count_based(self):
        """Ten threads is a minute on a fast account, ten on a slow one."""
        from quiport import harvest as H
        import inspect
        src = inspect.getsource(H.harvest)
        self.assertIn("i % 10 == 0 or i == len(pending) or beat.due()", src)

    def test_the_bulk_metadata_phase_reports(self):
        """128 calls before any per-thread work, right after the banner."""
        from quiport import harvest as H
        import inspect
        src = inspect.getsource(H._fetch_metadata)
        self.assertIn("_Heartbeat", src)
        self.assertIn("bulk metadata: batch %d/%d", src)

    def test_the_pacing_sleep_is_not_silent(self):
        from quiport import quipclient as Q
        import inspect
        src = inspect.getsource(Q.RateLimiter.wait_if_needed)
        self.assertIn("Pacing against", src)
        # announce before sleeping, not after
        self.assertLess(src.index("Pacing against"), src.rindex("time.sleep(delay)"))


class TestDiscoveryCompleteness(unittest.TestCase):
    """Discovery must never under-report the account without saying so."""

    def setUp(self):
        from quiport.config import Config
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    class FakeClient:
        """Answers folder/user calls from a script; `fail` forces a status."""

        def __init__(self, folders, fail=None, user=None):
            self.folders, self.fail, self.calls = folders, fail or {}, []
            self.user = user or {"id": "U1", "name": "T", "private_folder_id": "F1"}

        def get(self, endpoint, **params):
            from quiport.quipclient import Response
            self.calls.append((endpoint, params))
            ids = str(params.get("ids", ""))
            status = self.fail.get(ids)
            if status:
                return Response(status, {}, b'{"error":"x"}', endpoint, params)
            if endpoint == "/1/users/current":
                return Response(200, {}, json.dumps(self.user).encode(), endpoint, params)
            data = {i: self.folders[i] for i in ids.split(",") if i in self.folders}
            return Response(200, {}, json.dumps(data).encode(), endpoint, params)

        def get_json(self, endpoint, **params):
            r = self.get(endpoint, **params)
            return r.json() if r.ok else None

    @staticmethod
    def _folder(fid, children=(), threads=()):
        return {"folder": {"id": fid, "title": fid, "folder_type": "private"},
                "children": ([{"folder_id": c} for c in children]
                             + [{"thread_id": t} for t in threads])}

    def test_a_failed_batch_is_not_mistaken_for_restricted_folders(self):
        """regression: a 500 on one batch call marked all 50 folders restricted
        and dropped every subtree beneath them, reported as a permissions
        problem."""
        from quiport import discover as D
        client = self.FakeClient({"F1": self._folder("F1")}, fail={"F1": 500})
        with self.assertRaises(RuntimeError) as caught:
            D.discover(client, self.cfg)
        self.assertIn("no answer", str(caught.exception))

    def test_a_batch_failure_falls_back_to_asking_one_at_a_time(self):
        """One unreachable folder must not cost the other 49."""
        from quiport import discover as D
        folders = {f"F{i}": self._folder(f"F{i}") for i in range(1, 4)}
        client = self.FakeClient(folders, fail={"F1,F2,F3": 502})
        data, unanswered = D._fetch_folder_batch(client, ["F1", "F2", "F3"])
        self.assertEqual(unanswered, [])
        self.assertEqual(set(data), {"F1", "F2", "F3"})

    def test_a_genuine_refusal_is_recorded_not_retried_forever(self):
        from quiport import discover as D
        folders = {"F2": self._folder("F2")}
        client = self.FakeClient(folders, fail={"F1,F2": 500, "F1": 403})
        data, unanswered = D._fetch_folder_batch(client, ["F1", "F2"])
        self.assertEqual(unanswered, [], "403 is an answer, not a failure")
        self.assertIn("error", data["F1"])
        self.assertIn("folder", data["F2"])

    def test_a_clean_walk_records_that_it_completed(self):
        from quiport import discover as D
        folders = {"F1": self._folder("F1", children=["F2"], threads=["T1"]),
                   "F2": self._folder("F2", threads=["T2"])}
        client = self.FakeClient(folders)
        client.get_json = lambda e, **p: (   # sweeps return nothing to add
            {} if "threads" in e or "recent" in e else client.get(e, **p).json())
        summary = D.discover(client, self.cfg)
        self.assertTrue(summary["discovery_complete"])
        self.assertEqual(summary["folders"], 2)
        self.assertEqual(summary["threads"], 2)

    def test_rediscovery_does_not_forget_threads_refresh_added(self):
        """regression: `discover` rewrote threads.jsonl wholesale, so two
        documents added by `refresh` -- which no folder lists -- vanished from
        the export and the next render deleted their pages as stale."""
        from quiport import discover as D
        from quiport.state import JsonlFile

        threads = JsonlFile(self.cfg.state_dir / "threads.jsonl")
        threads.write_all([{"id": "ADDED_BY_REFRESH", "folder_ids": []}])

        folders = {"F1": self._folder("F1", threads=["T1"])}
        client = self.FakeClient(folders)
        client.get_json = lambda e, **p: (
            {} if "threads" in e or "recent" in e else client.get(e, **p).json())
        summary = D.discover(client, self.cfg)

        listed = {r["id"] for r in threads.read_all()}
        self.assertIn("ADDED_BY_REFRESH", listed, "must not be dropped")
        self.assertIn("T1", listed)
        self.assertEqual(summary["carried_forward"], 1)
        self.assertEqual(summary["threads"], 2)
        self.assertEqual(summary["threads_reached_by_this_walk"], 1)

    def test_rediscovery_merges_folder_ids_rather_than_replacing_them(self):
        from quiport import discover as D
        from quiport.state import JsonlFile
        threads = JsonlFile(self.cfg.state_dir / "threads.jsonl")
        threads.write_all([{"id": "T1", "folder_ids": ["OLD"]}])
        client = self.FakeClient({"F1": self._folder("F1", threads=["T1"])})
        client.get_json = lambda e, **p: (
            {} if "threads" in e or "recent" in e else client.get(e, **p).json())
        D.discover(client, self.cfg)
        rec = next(r for r in threads.read_all() if r["id"] == "T1")
        self.assertEqual(rec["folder_ids"], ["OLD", "F1"])

    def test_audit_notices_a_harvested_thread_missing_from_the_list(self):
        """No other check can see this: the audit verifies what is *in* the
        list, and a thread that fell out of it is simply not examined."""
        from quiport import audit as A
        from quiport.state import JsonlFile
        JsonlFile(self.cfg.state_dir / "threads.jsonl").write_all(
            [{"id": "STILL_HERE", "folder_ids": []}])
        JsonlFile(self.cfg.state_dir / "ledger.jsonl").write_all([
            {"phase": "harvest", "unit": "STILL_HERE", "status": "done"},
            {"phase": "harvest", "unit": "FORGOTTEN", "status": "done"},
        ])
        gaps = A.audit_forgotten_threads(self.cfg)
        self.assertEqual(len(gaps), 1)
        self.assertIn("FORGOTTEN", gaps[0][1])

    def test_the_sweep_asks_three_independent_questions(self):
        """Finding every thread matters more than finishing quickly, so all
        three enumerations run by default and their results are unioned."""
        from quiport import discover as D
        import inspect
        src = inspect.getsource(D._sweep_unfiled)
        self.assertIn("/1/users/current/threads", src)
        self.assertIn("threads-modified-after-usec", src)
        self.assertIn('threads_meta="true"', src)
        self.assertIn("deep: bool = True", src)
        self.assertIn('client.get("/1/threads/recent"',
                      inspect.getsource(D._sweep_recent))

    def test_the_expensive_sweep_is_default_on_and_opt_out(self):
        from quiport import discover as D, config as C
        import inspect
        self.assertIn("deep=not getattr(cfg, 'quick_sweep', False)",
                      inspect.getsource(D.discover))
        self.assertFalse(C.Config().quick_sweep)

    def test_the_sweep_reports_progress(self):
        """Dozens of calls with no output read as a hang."""
        from quiport import discover as D
        import inspect
        self.assertIn("sweeping %s: page %d", inspect.getsource(D._sweep_unfiled))

    def test_the_slow_sweep_announces_itself_before_stalling(self):
        """It downloads every document's content; minutes of silence look
        like a hang, so it says so before it starts."""
        from quiport import discover as D
        import inspect, logging
        src = inspect.getsource(D._sweep_unfiled)
        announce = src.index("/1/threads/recent")
        start = src.index("_sweep_recent(client")
        self.assertLess(announce, start, "warn before the wait, not after")
        self.assertIn("not stuck", src.lower())

    def test_the_sweep_finds_threads_no_folder_lists(self):
        from quiport import discover as D
        from quiport.quipclient import Response

        # The first endpoint is not limited to threads the user authored;
        # FOREIGN stands for one someone else wrote and shared.
        pages = {"/1/users/current/threads": {"threads": {"KNOWN": {}, "FOREIGN": {}}},
                 "/1/users/current/threads-modified-after-usec":
                     {"threads": {"KNOWN": {}, "UNFILED": {}}},
                 "/1/threads/recent": {"RECENTONLY": {"thread": {"id": "RECENTONLY",
                                                                 "updated_usec": 5}}}}

        class C:
            def __init__(self):
                self.asked = []

            def get(self, endpoint, **params):
                self.asked.append(endpoint)
                body = pages[endpoint]
                if endpoint == "/1/threads/recent" and params.get("max_updated_usec"):
                    body = {}
                return Response(200, {}, json.dumps(body).encode(), endpoint, params)

        c = C()
        found = D._sweep_unfiled(c, known={"KNOWN"})
        self.assertEqual(found, {"FOREIGN", "UNFILED", "RECENTONLY"})
        self.assertIn("/1/threads/recent", c.asked)

        c = C()
        found = D._sweep_unfiled(c, known={"KNOWN"}, deep=False)
        self.assertEqual(found, {"FOREIGN", "UNFILED"})
        self.assertNotIn("/1/threads/recent", c.asked)

    def test_a_failed_sweep_page_stops_rather_than_truncating(self):
        """`if not page: break` treated a failed call as "no more pages", so a
        blip silently shortened the list of unfiled threads."""
        from quiport import discover as D
        from quiport.quipclient import Response

        class Failing:
            def get(self, endpoint, **params):
                return Response(500, {}, b'{"error":"x"}', endpoint, params)

        with self.assertRaises(RuntimeError) as caught:
            D._sweep_unfiled(Failing(), set())
        self.assertIn("failed", str(caught.exception))


class TestInterruptionSafety(unittest.TestCase):
    """What must hold when a harvest is killed and resumed hours later."""

    def setUp(self):
        from quiport.config import Config
        from quiport.rawstore import RawStore
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.raw = RawStore(self.cfg.raw_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _page(self, cursor, next_cursor, html):
        params = {"cursor": cursor} if cursor else {}
        body = json.dumps({"html": html,
                           "response_metadata": {"next_cursor": next_cursor}})
        self.raw.save("GET", "/2/threads/T1/html", params, 200, {}, body.encode())

    # -- cursor chains ------------------------------------------------

    def test_a_terminated_chain_is_complete(self):
        self._page(None, "c1", "<p>one</p>")
        self._page("c1", "", "<p>two</p>")
        chain = self.raw.html_chain("T1")
        self.assertTrue(chain["complete"])
        self.assertEqual(chain["html"], "<p>one</p><p>two</p>")
        self.assertEqual(chain["pages"], 2)

    def test_a_chain_still_pointing_forward_is_incomplete(self):
        """The bug this exists for: 40% of a document, rendered as if whole."""
        self._page(None, "c1", "<p>one</p>")
        chain = self.raw.html_chain("T1")
        self.assertFalse(chain["complete"])
        self.assertIn("page 2 missing", chain["reason"])
        self.assertEqual(chain["html"], "<p>one</p>")   # the prefix is still returned

    def test_never_fetched_is_incomplete_not_empty_and_fine(self):
        chain = self.raw.html_chain("T1")
        self.assertFalse(chain["complete"])
        self.assertEqual(chain["html"], "")

    def test_a_403_on_the_first_page_is_a_refusal_not_a_broken_chain(self):
        self.raw.save("GET", "/2/threads/T1/html", {}, 403, {}, b'{"error":"no"}')
        chain = self.raw.html_chain("T1")
        self.assertFalse(chain["complete"])
        self.assertEqual(chain["status"], 403)

    def test_a_failure_after_page_one_is_reported_as_retryable(self):
        """A cursor is a temporary handle; a mid-chain error is never final."""
        self._page(None, "c1", "<p>one</p>")
        self.raw.save("GET", "/2/threads/T1/html", {"cursor": "c1"}, 400, {},
                      b'{"error_description":"something else"}')
        chain = self.raw.html_chain("T1")
        self.assertFalse(chain["complete"])
        self.assertEqual(chain["status"], 0, "must not look like a permanent answer")

    # -- expired cursors ----------------------------------------------

    def test_an_expired_cursor_400_is_never_served_from_cache(self):
        """Measured on the live API. Cached as a normal 400 it makes the chain
        permanently unfinishable, because the cursor is deterministic and page
        one keeps handing back the same dead one."""
        from quiport.quipclient import is_expired_cursor
        body = b'{"error_code":400,"error_description":"invalid or expired cursor: QWJH="}'
        self.assertTrue(is_expired_cursor(400, body))
        self.raw.save("GET", "/2/threads/T1/members", {"cursor": "c1"}, 400, {}, body)
        self.assertIsNone(self.raw.load("GET", "/2/threads/T1/members", {"cursor": "c1"}))

    def test_an_ordinary_400_is_still_a_cached_answer(self):
        self.raw.save("GET", "/1/threads/T1/export/xlsx", {}, 400, {},
                      b'{"error":"application/xlsx not supported"}')
        got = self.raw.load("GET", "/1/threads/T1/export/xlsx", {})
        self.assertIsNotNone(got)
        self.assertEqual(got.status, 400)

    # -- index durability ---------------------------------------------

    def test_a_torn_index_line_does_not_corrupt_the_next_entry(self):
        """A power loss mid-append leaves no trailing newline. Appending straight
        after it fused the next -- good -- response onto the fragment, hiding it
        from the renderer entirely."""
        self.raw.save("GET", "/a", {}, 200, {}, b"first")
        with self.raw.index_path.open("a", encoding="utf-8") as f:
            f.write('{"key": "torn", "canonical": "GET /1/th')      # no newline
        self.raw.save("GET", "/b", {}, 200, {}, b"second")

        good = [r for r in self.raw.iter_index() if r.get("canonical") == "GET /b"]
        self.assertEqual(len(good), 1, "the entry after a torn line must survive")

    def test_reindex_rebuilds_from_the_files(self):
        for i in range(5):
            self.raw.save("GET", f"/x{i}", {}, 200, {}, b"body")
        self.raw.index_path.write_text("")                # simulate a lost index
        self.assertEqual(len(list(self.raw.iter_index())), 0)
        stats = self.raw.reindex()
        self.assertEqual(stats["entries"], 5)
        self.assertEqual(len(list(self.raw.iter_index())), 5)

    def test_reindex_ignores_archived_copies(self):
        self.raw.save("GET", "/a", {}, 200, {}, b"one")
        self.raw.save("GET", "/a", {}, 200, {}, b"two")     # supersedes
        self.assertEqual(self.raw.reindex()["entries"], 1)


class TestPaginationSelfHealing(unittest.TestCase):
    """A stale cursor must not make a chain unfinishable."""

    def setUp(self):
        from quiport.config import Config
        from quiport.rawstore import RawStore
        from quiport.quipclient import QuipClient, Response
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        cfg.ensure_dirs()
        self.raw = RawStore(cfg.raw_dir)
        self.Response = Response

        self.attempts = []
        client = QuipClient("tok", store=self.raw)
        expired = (b'{"error_code":400,"error_description":'
                   b'"invalid or expired cursor: c1"}')

        def fake_fetch(endpoint, params):
            self.attempts.append(dict(params))
            cursor = params.get("cursor")
            if cursor is None:
                return Response(200, {}, json.dumps(
                    {"members": [1], "response_metadata": {"next_cursor": "c1"}}
                ).encode(), endpoint, params)
            # The first time the continuation is asked for, the cursor is dead;
            # after the chain restarts, the same cursor works.
            if self.attempts.count({"cursor": "c1"}) == 1:
                return Response(400, {}, expired, endpoint, params)
            return Response(200, {}, json.dumps(
                {"members": [2], "response_metadata": {"next_cursor": ""}}
            ).encode(), endpoint, params)

        client._fetch = fake_fetch
        self.client = client

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_chain_restarts_and_completes(self):
        pages, complete = self.client.paginate_all("/2/threads/T1/members")
        self.assertTrue(complete, "an expired cursor must not end the chain")
        self.assertEqual(len(pages), 2)

    def test_the_expired_response_is_not_left_blocking_the_cache(self):
        self.client.paginate_all("/2/threads/T1/members")
        got = self.raw.load("GET", "/2/threads/T1/members", {"cursor": "c1"})
        self.assertIsNotNone(got)
        self.assertTrue(got.ok, "the successful retry must be what is cached")

    def test_a_first_page_refusal_does_not_loop(self):
        from quiport.quipclient import QuipClient, Response
        client = QuipClient("tok", store=self.raw)
        client._fetch = lambda e, p: Response(403, {}, b'{"error":"no"}', e, p)
        pages, complete = client.paginate_all("/2/threads/T9/members")
        self.assertEqual(pages, [])
        self.assertTrue(complete, "403 is final; there is nothing more to fetch")


class TestHarvestRefusesPartialResults(unittest.TestCase):
    """A thread is only 'done' when everything about it actually arrived."""

    def setUp(self):
        from quiport.config import Config
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def test_incomplete_html_raises_so_the_thread_stays_pending(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Long doc")

        class Client:
            def paginate_all(self, endpoint, **kw):
                return [{"html": "<p>half</p>",
                         "response_metadata": {"next_cursor": "more"}}], False

        with self.assertRaises(RuntimeError) as caught:
            Hv._fetch_content(Client(), thread, self.cfg, {})
        self.assertIn("incomplete", str(caught.exception))

    def test_complete_html_does_not_raise(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc")

        class Client:
            def paginate_all(self, endpoint, **kw):
                return [{"html": "<p>all</p>",
                         "response_metadata": {"next_cursor": ""}}], True

        Hv._fetch_content(Client(), thread, self.cfg, {})
        self.assertEqual(thread.html, "<p>all</p>")
        self.assertTrue(thread.html_complete)

    def test_incomplete_sharing_raises(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc")

        class Client:
            def paginate_all(self, endpoint, **kw):
                complete = "members" not in endpoint
                return [{"members": [], "folders": [], "invited_members": []}], complete

        with self.assertRaises(RuntimeError) as caught:
            Hv._fetch_sharing(Client(), thread)
        self.assertIn("members", str(caught.exception))

    def _client(self, status, body=b'{"error":"x"}'):
        from quiport.quipclient import Response

        class C:
            store = None
            def get(self, endpoint, **params):
                return Response(status, {}, body, endpoint, params)
        return C()

    def test_a_failed_message_page_raises_rather_than_truncating(self):
        """regression: `break` on failure was indistinguishable from reaching
        the end, so a blip silently shortened the comment history and the thread
        was still recorded as fully harvested."""
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc")
        with self.assertRaises(Hv.Incomplete):
            Hv._fetch_messages(self._client(500), thread, {"messages": 0})

    def test_a_403_on_messages_is_final_not_a_failure(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc")
        Hv._fetch_messages(self._client(403), thread, {"messages": 0})
        self.assertIn("messages unavailable (HTTP 403)", thread.errors)

    def test_a_blob_that_never_answered_raises(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc",
                        html="<img src='/blob/T1/B1'>")
        stats = {"blobs": 0, "blob_errors": 0}
        with self.assertRaises(Hv.Incomplete):
            Hv._fetch_blobs(self._client(502), thread, self.cfg, stats)

    def test_a_403_blob_is_recorded_not_raised(self):
        """Documented: blob permissions are independent of thread permissions."""
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc", html="<img src='/blob/T1/B1'>")
        stats = {"blobs": 0, "blob_errors": 0}
        Hv._fetch_blobs(self._client(403), thread, self.cfg, stats)
        self.assertEqual(stats["blob_errors"], 1)

    def test_an_xlsx_that_never_answered_raises(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Sheet", spreadsheet_summary={"num_tables": 1})
        with self.assertRaises(Hv.Incomplete):
            Hv._fetch_xlsx(self._client(503), thread, {"xlsx": 0, "xlsx_errors": 0})

    def test_a_400_xlsx_is_final_not_a_failure(self):
        from quiport import harvest as Hv
        thread = Thread(id="T1", title="Doc", spreadsheet_summary={"num_tables": 1})
        stats = {"xlsx": 0, "xlsx_errors": 0}
        Hv._fetch_xlsx(self._client(400), thread, stats)
        self.assertEqual(stats["xlsx_errors"], 1)

    def test_check_updates_refuses_to_report_a_short_list(self):
        """A failed page would look like "no more changes", and the operator
        would approve an incomplete list believing it was everything."""
        from quiport import refresh as Rf
        with self.assertRaises(RuntimeError) as caught:
            Rf.find_updates(self._client(500), self.cfg)
        self.assertIn("incomplete", str(caught.exception))

    def test_a_401_stops_the_run_instead_of_marking_threads_bad(self):
        """An expiring token would otherwise mark the rest of the account as
        errors and exit zero."""
        from quiport.quipclient import QuipAuthError
        self.assertTrue(issubclass(QuipAuthError, Exception))
        # harvest re-raises it rather than treating it as a per-thread failure
        import inspect
        from quiport import harvest as Hv
        src = inspect.getsource(Hv.harvest)
        self.assertIn("except q.QuipAuthError", src)
        self.assertLess(src.index("except q.QuipAuthError"),
                        src.index("except Exception"),
                        "the auth handler must come first or it is unreachable")


class TestXlsxExport(unittest.TestCase):
    """The real workbook beside every spreadsheet-bearing document."""

    MINIMAL_XLSX = b"PK\x03\x04" + b"\x00" * 40      # enough to pass the magic check

    def setUp(self):
        from quiport.config import Config
        from quiport.rawstore import RawStore
        from quiport.render.blobs import BlobStore
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.raw = RawStore(self.cfg.raw_dir)
        self.blobs = BlobStore(self.cfg, self.raw, "Imported from Quip")
        self.thread = Thread(id="T1", secret_path="abc123", title="Q3 Numbers",
                             type="spreadsheet",
                             spreadsheet_summary={"num_tables": 3, "has_formulas": True})

    def tearDown(self):
        self.tmp.cleanup()

    def _cache(self, body, status=200):
        self.raw.save("GET", "/1/threads/T1/export/xlsx", {}, status, {}, body)

    def test_workbook_is_written_and_linked(self):
        self._cache(self.MINIMAL_XLSX)
        rel = self.blobs.materialize_xlsx(self.thread, "Imported from Quip/A/doc.html")
        self.assertEqual(rel, "../_files/abc123/Q3-Numbers.xlsx")
        self.assertTrue((self.cfg.root / self.thread.xlsx_path).exists())

    def test_path_is_recorded_on_the_thread_for_the_reports(self):
        self._cache(self.MINIMAL_XLSX)
        self.blobs.materialize_xlsx(self.thread, "doc.html")
        self.assertEqual(self.thread.xlsx_path,
                         "Imported from Quip/_files/abc123/Q3-Numbers.xlsx")

    def test_an_error_response_is_not_written_as_a_workbook(self):
        """A 400 body is JSON; writing it as .xlsx would produce a file that
        looks real and opens as garbage."""
        self._cache(b'{"error":"application/xlsx not supported"}', status=400)
        self.assertIsNone(self.blobs.materialize_xlsx(self.thread, "doc.html"))
        self.assertEqual(self.thread.xlsx_path, "")

    def test_a_200_that_is_not_a_zip_is_rejected(self):
        self._cache(b'{"error":"nope"}', status=200)
        self.assertIsNone(self.blobs.materialize_xlsx(self.thread, "doc.html"))

    def test_nothing_cached_means_no_link(self):
        self.assertIsNone(self.blobs.materialize_xlsx(self.thread, "doc.html"))

    def test_only_spreadsheet_bearing_threads_are_fetched(self):
        """Quip rejects the rest with 400, so spending the call is pointless."""
        from quiport import harvest as Hv
        calls = []

        class FakeClient:
            def get(self, path, **kw):
                calls.append(path)
                from quiport.quipclient import Response
                return Response(200, {}, b"PK\x03\x04", path)

        stats = {"xlsx": 0, "xlsx_errors": 0}
        plain = Thread(id="T2", title="Just prose")
        Hv._fetch_xlsx(FakeClient(), plain, stats)
        self.assertEqual(calls, [])

        Hv._fetch_xlsx(FakeClient(), self.thread, stats)
        self.assertEqual(calls, ["/1/threads/T1/export/xlsx"])
        self.assertEqual(stats["xlsx"], 1)

    def test_chats_are_never_asked_for_a_workbook(self):
        from quiport import harvest as Hv
        chat = Thread(id="T3", title="Room", type="chat",
                      spreadsheet_summary={"num_tables": 1})
        calls = []

        class FakeClient:
            def get(self, path, **kw):
                calls.append(path)

        Hv._fetch_xlsx(FakeClient(), chat, {"xlsx": 0, "xlsx_errors": 0})
        self.assertEqual(calls, [])


class TestFolderMembershipMerge(unittest.TestCase):
    """A thread added by `refresh` knows its folder before the folder knows it."""

    def _ctx_and_threads(self, folder_children, thread_folders):
        from quiport.model import Folder
        from quiport.render import build_tree

        class Ctx:
            content_dir_name = "Imported from Quip"
            folders = {"F1": Folder(id="F1", title="Private",
                                    child_thread_ids=list(folder_children))}

        t = Thread(id="T1", secret_path="abc123", title="New Doc")
        t.folder_ids = list(thread_folders)
        return build_tree(Ctx(), {"T1": t}), t

    def test_thread_is_filed_when_only_the_thread_knows(self):
        """regression: discovery snapshots child_thread_ids, so a thread added
        later is absent from it and used to land at the content root."""
        (_alloc, _folders), t = self._ctx_and_threads(folder_children=[],
                                                      thread_folders=["F1"])
        self.assertEqual(t.output_path,
                         "Imported from Quip/Private/abc123-New-Doc.html")
        self.assertEqual(t.folder_paths, ["Imported from Quip/Private"])

    def test_agreement_between_both_sides_files_it_once(self):
        (_alloc, _folders), t = self._ctx_and_threads(folder_children=["T1"],
                                                      thread_folders=["F1"])
        self.assertEqual(t.folder_paths, ["Imported from Quip/Private"])

    def test_a_genuinely_unfiled_thread_still_gets_a_home(self):
        (_alloc, _folders), t = self._ctx_and_threads(folder_children=[],
                                                      thread_folders=[])
        self.assertEqual(t.output_path, "Imported from Quip/abc123-New-Doc.html")
        self.assertEqual(t.folder_paths, [])

    def test_unknown_folder_id_is_ignored_not_crashed_on(self):
        (_alloc, _folders), t = self._ctx_and_threads(folder_children=[],
                                                      thread_folders=["GONE"])
        self.assertEqual(t.output_path, "Imported from Quip/abc123-New-Doc.html")


class TestStalePagePruning(unittest.TestCase):
    def setUp(self):
        from quiport.config import Config
        from quiport.render import _prune_stale_pages
        self.prune = _prune_stale_pages
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(export_id="t", base_dir=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, text="x"):
        p = self.cfg.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_a_page_the_render_no_longer_produces_is_removed(self):
        """A moved document leaves a stale duplicate that still looks real."""
        keep = self._write("Imported from Quip/Private/abc-Doc.html")
        stale = self._write("Imported from Quip/abc-Doc.html")
        removed = self.prune(self.cfg, {"Imported from Quip/Private/abc-Doc.html"})
        self.assertEqual(removed, 1)
        self.assertTrue(keep.exists())
        self.assertFalse(stale.exists())

    def test_blobs_are_never_touched(self):
        blob = self._write("Imported from Quip/_files/abc/photo.png")
        page = self._write("Imported from Quip/_files/abc/notes.html")
        self.prune(self.cfg, set())
        self.assertTrue(blob.exists())
        self.assertTrue(page.exists(), "_files is off limits")

    def test_empty_folders_are_cleaned_up(self):
        self._write("Imported from Quip/Gone/index.html")
        self.prune(self.cfg, set())
        self.assertFalse((self.cfg.content_dir / "Gone").exists())


class TestExportIdGuard(unittest.TestCase):
    """A mistyped --export-id must not be mistaken for a fresh account."""

    def setUp(self):
        from quiport import cli
        self.cli = cli
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "export-myaccount").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, export_id):
        return argparse.Namespace(export_id=export_id, base_dir=str(self.base),
                                  token="t", base_url=None, sharing="full",
                                  retry_errors=False)

    def test_missing_export_exits_and_suggests_the_real_one(self):
        with self.assertRaises(SystemExit) as caught:
            self.cli._setup(self._args("sw-quip"), need_export=True)
        self.assertIn("--export-id myaccount", str(caught.exception))

    def test_missing_export_is_not_silently_created(self):
        with self.assertRaises(SystemExit):
            self.cli._setup(self._args("sw-quip"), need_export=True)
        self.assertFalse((self.base / "export-sw-quip").exists())

    def test_existing_export_passes(self):
        cfg, _store, _ledger, _client = self.cli._setup(
            self._args("myaccount"), need_export=True)
        self.assertEqual(cfg.export_id, "myaccount")


class TestQuipDocumentStyling(unittest.TestCase):
    """The document look mirrors Quip's own export stylesheet.

    Values here come from quip-export/lib/templates/document.css (Apache 2.0),
    which is Quip's published rendering of its own documents.
    """

    @classmethod
    def setUpClass(cls):
        from quiport.render.templates import STYLESHEET
        cls.css = STYLESHEET

    def rule(self, selector: str) -> str:
        """The declarations of the rule with this exact selector.

        Anchored at a line start: `.quip-body q` is a substring of
        `.quip-body blockquote, .quip-body q`, so a bare find matches the wrong
        rule and quietly asserts nothing.
        """
        needle = "\n" + selector + " {"
        i = self.css.find(needle)
        self.assertNotEqual(i, -1, f"no rule for {selector}")
        return self.css[i + len(needle) : self.css.index("}", i)]

    def test_headings_are_not_small_caps(self):
        """Quip renders h3 as a 13px uppercase label; that was rolled back."""
        body = self.css[self.css.index("DOCUMENT PAGES"):
                        self.css.index("---- live apps ----")]
        self.assertNotIn("text-transform: uppercase", body)

    def test_paragraphs_carry_no_margin(self):
        """Quip stores a blank line as an empty <p>; default margins triple the gap."""
        self.assertIn("margin: 0", self.rule(".quip-body p"))

    def test_blockquote_matches_quip(self):
        quote = self.rule(".quip-body blockquote")
        self.assertIn("border-left: 4px solid #a6a6a6", quote)
        self.assertIn("padding-left: 10px", quote)
        self.assertIn("font-style: italic",
                      self.rule(".quip-body blockquote, .quip-body q"))

    def test_pull_quote_matches_quip(self):
        pull = self.rule(".quip-body q")
        for declaration in ("color: #a6a6a6", "font-size: 24px",
                            "text-align: center", "quotes: none",
                            "line-height: 40px"):
            self.assertIn(declaration, pull)

    def test_pull_quote_is_a_block_not_an_inline_quotation(self):
        self.assertIn("display: block", self.rule(".quip-body blockquote, .quip-body q"))
        self.assertIn('content: ""', self.rule(".quip-body q::before, .quip-body q::after"))

    def test_quip_rules_do_not_escape_the_document_body(self):
        """The metadata block is itself a <blockquote>; an unscoped rule would
        italicise it and give it Quip's grey bar.

        The chrome's own bare `h1`/`h2`/`h3` sizes are deliberate and stay --
        every property they set is overridden inside .quip-body."""
        for selector in ("\nblockquote {", "\nq {", "\np {"):
            self.assertNotIn(selector, self.css)

    def test_content_pages_pin_the_light_palette(self):
        """Quip is light-only, so a document must not follow a dark system theme."""
        content = self.rule(
            "body.quip-document, body.quip-chat-page, body.quip-stub-page")
        self.assertIn("--bg: #ffffff", content)
        self.assertIn("--fg: #333", content)
        self.assertIn("max-width: 900px", content)


class TestFolderToggle(unittest.TestCase):
    def test_button_uses_the_triangle_icon(self):
        from quiport.reports import _toggle_button
        self.assertIn('href="#quip-icon-triangle-right"', _toggle_button())

    def test_sprite_defines_the_triangle_the_button_references(self):
        """A <use> with no matching <symbol> renders blank space, not an error."""
        from quiport.render.templates import icon_sprite
        self.assertIn('id="quip-icon-triangle-right"', icon_sprite())
        self.assertIn('viewBox="0 0 12 12"', icon_sprite())

    def test_ui_icons_stay_out_of_the_thread_type_map(self):
        """ICON_BY_KIND is the set of thread types; unknown_icon_kinds depends on it."""
        from quiport.render.templates import ICON_BY_KIND, UI_ICONS
        self.assertFalse(set(UI_ICONS) & set(ICON_BY_KIND))

    def test_expanded_state_rotates_the_icon(self):
        from quiport.render.templates import STYLESHEET
        self.assertIn('.quip-toggle[aria-expanded="true"] .quip-toggle-icon '
                      "{ transform: rotate(90deg); }", STYLESHEET)
        self.assertIn("fill: rgb(51, 51, 51)", STYLESHEET)

    def test_icon_never_swallows_a_click(self):
        """Without this the click target is the <svg>, not the button."""
        from quiport.render.templates import STYLESHEET
        icon_rule = STYLESHEET.split(".quip-toggle-icon {", 1)[1].split("}", 1)[0]
        self.assertIn("pointer-events: none", icon_rule)

    def test_path_to_root(self):
        self.assertEqual(naming.path_to_root("index.html"), "")
        self.assertEqual(naming.path_to_root("Imported from Quip/A/index.html"), "../../")
        self.assertEqual(naming.path_to_root("Imported from Quip/A/B/index.html"), "../../../")


class TestReportIcons(unittest.TestCase):
    """Report tables carry a type icon beside each title."""

    @staticmethod
    def _thread(type_, title="T", path="a/b.html"):
        t = Thread(id="x", title=title, type=type_)
        t.output_path = path
        return t

    def test_icon_follows_the_thread_type_not_the_report(self):
        """A document with an embedded spreadsheet is still a document.

        It appears in spreadsheets.html, but showing it with a spreadsheet
        icon there would misdescribe what it is.
        """
        from quiport.reports import _title_cell
        doc = self._thread("document")
        doc.spreadsheet_summary = {"has_formulas": True}
        self.assertTrue(doc.has_embedded_spreadsheet)
        self.assertIn("#quip-icon-document", _title_cell("recent.html", doc))

    def test_standalone_spreadsheet_gets_the_spreadsheet_icon(self):
        from quiport.reports import _title_cell
        sheet = self._thread("spreadsheet")
        sheet.spreadsheet_summary = {}
        self.assertIn("#quip-icon-spreadsheet", _title_cell("recent.html", sheet))

    def test_every_type_maps_to_its_own_icon(self):
        from quiport.reports import _title_cell
        for kind in ("document", "spreadsheet", "slides", "chat"):
            self.assertIn(f"#quip-icon-{kind}",
                          _title_cell("recent.html", self._thread(kind)), kind)

    def test_title_stays_linked(self):
        from quiport.reports import _title_cell
        out = _title_cell("recent.html", self._thread("document", "My Doc", "x/y.html"))
        self.assertIn('<a href="x/y.html">My Doc</a>', out)

    def test_icon_label_for_rows_with_no_local_page(self):
        from quiport.reports import _icon_label
        out = _icon_label("folder", "FldAOAxmpl2")
        self.assertIn("#quip-icon-folder", out)
        self.assertIn("FldAOAxmpl2", out)
        self.assertNotIn("<a ", out)

    def test_unmapped_type_is_surfaced_not_absorbed(self):
        """An unknown type renders as a document, so it must be reported."""
        from quiport.reports import unknown_icon_kinds
        threads = {"a": self._thread("document"), "b": self._thread("hologram")}
        self.assertEqual(unknown_icon_kinds(threads), {"hologram"})

    def test_known_types_produce_no_warning(self):
        from quiport.reports import unknown_icon_kinds
        threads = {k: self._thread(k) for k in
                   ("document", "spreadsheet", "slides", "chat")}
        self.assertEqual(unknown_icon_kinds(threads), set())


class TestRefreshTargets(unittest.TestCase):
    def test_parses_a_document_url(self):
        from quiport.refresh import parse_target
        self.assertEqual(
            parse_target("https://acme.quip.com/Sp4xmplPath0/Live-apps-test"),
            "Sp4xmplPath0")

    def test_parses_url_without_title_or_scheme(self):
        from quiport.refresh import parse_target
        self.assertEqual(parse_target("https://acme.quip.com/Sp3xmplPath0"),
                         "Sp3xmplPath0")
        self.assertEqual(parse_target("acme.quip.com/Sp3xmplPath0/Rat"),
                         "Sp3xmplPath0")

    def test_accepts_a_bare_id(self):
        from quiport.refresh import parse_target
        self.assertEqual(parse_target("DocAAAxmpl1"), "DocAAAxmpl1")

    def test_ignores_surrounding_whitespace(self):
        from quiport.refresh import parse_target
        self.assertEqual(parse_target("  https://x.quip.com/abcDEF12345/T  "),
                         "abcDEF12345")


class TestSupersede(unittest.TestCase):
    """A refresh must not destroy the response it replaces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RawStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_changed_response_archives_the_previous_one(self):
        self.store.save("GET", "/2/threads/T/html", {}, 200, {}, b'{"html":"old"}')
        self.store.save("GET", "/2/threads/T/html", {}, 200, {}, b'{"html":"new"}')
        self.assertEqual(self.store.load("GET", "/2/threads/T/html", {}).body,
                         b'{"html":"new"}')
        archived = list(self.store.root.rglob("superseded/*.body"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), b'{"html":"old"}')

    def test_identical_response_archives_nothing(self):
        """Re-fetching an unchanged document should not litter the store."""
        for _ in range(3):
            self.store.save("GET", "/2/threads/T/html", {}, 200, {}, b'{"html":"same"}')
        self.assertEqual(self.store.superseded_count(), 0)

    def test_index_records_every_fetch_even_when_unchanged(self):
        for _ in range(3):
            self.store.save("GET", "/a", {}, 200, {}, b"same")
        self.assertEqual(self.store.stats()["calls"], 3)


class TestBulkMetadataLoading(unittest.TestCase):
    """regression: the renderer must not depend on harvest's batch boundaries.

    Batches are formed from the thread list, so adding one thread shifts every
    boundary. Reconstructing `?ids=` keys then missed the cache and silently
    dropped ~11 documents from a rebuild after `refresh`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RawStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_threads_are_found_regardless_of_how_they_were_batched(self):
        from quiport.render import _load_bulk_metadata
        # Harvest batched A,B together and C alone.
        self.store.save("GET", "/1/threads/", {"ids": "A,B"}, 200, {},
                        json.dumps({"A": {"thread": {"id": "A"}},
                                    "B": {"thread": {"id": "B"}}}).encode())
        self.store.save("GET", "/1/threads/", {"ids": "C"}, 200, {},
                        json.dumps({"C": {"thread": {"id": "C"}}}).encode())
        v1, _ = _load_bulk_metadata(self.store)
        self.assertEqual(set(v1), {"A", "B", "C"})

    def test_v1_and_v2_are_kept_apart(self):
        from quiport.render import _load_bulk_metadata
        self.store.save("GET", "/1/threads/", {"ids": "A"}, 200, {},
                        json.dumps({"A": {"thread": {"id": "A", "type": "document"}}}).encode())
        self.store.save("GET", "/2/threads/", {"ids": "A"}, 200, {},
                        json.dumps({"A": {"thread": {"id": "A", "secret_path": "s"}}}).encode())
        v1, v2 = _load_bulk_metadata(self.store)
        self.assertIn("A", v1)
        self.assertIn("A", v2)
        self.assertEqual(v2["A"]["thread"]["secret_path"], "s")

    def test_a_refetched_response_wins(self):
        from quiport.render import _load_bulk_metadata
        self.store.save("GET", "/1/threads/", {"ids": "A"}, 200, {},
                        json.dumps({"A": {"thread": {"id": "A", "title": "old"}}}).encode())
        self.store.save("GET", "/1/threads/", {"ids": "A"}, 200, {},
                        json.dumps({"A": {"thread": {"id": "A", "title": "new"}}}).encode())
        v1, _ = _load_bulk_metadata(self.store)
        self.assertEqual(v1["A"]["thread"]["title"], "new")

    def test_error_responses_are_ignored(self):
        from quiport.render import _load_bulk_metadata
        self.store.save("GET", "/1/threads/", {"ids": "A"}, 403, {}, b'{"error":"no"}')
        v1, _ = _load_bulk_metadata(self.store)
        self.assertEqual(v1, {})


class TestBrowserUtils(unittest.TestCase):
    """Exercise quip-utils.js under node, when node is available."""

    SCRIPT = Path(__file__).resolve().parent.parent / "quiport" / "assets" / "quip-utils.js"

    def setUp(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("node not installed")

    def _run(self, body):
        import subprocess
        harness = f"""
global.window = global;
global.document = {{readyState:'complete', addEventListener(){{}}, querySelectorAll(){{return []}}}};
require({str(self.SCRIPT)!r});
{body}
"""
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_relative_time_boundaries(self):
        cases = [(1786838390000000, "just now"), (1786838340000000, "1 minute ago"),
                 (1786835700000000, "45 minutes ago"), (1786820400000000, "5 hours ago"),
                 (1786752000000000, "1 day ago"), (1786579200000000, "3 days ago"),
                 (1785628800000000, "2 weeks ago"), (1778630400000000, "3 months ago"),
                 (1723766400000000, "2 years ago")]
        js = "const NOW = Date.UTC(2026,7,16);" + "".join(
            f"console.log(QuipUtils.relativeTime({u}, NOW));" for u, _ in cases)
        got = self._run(js).splitlines()
        self.assertEqual(got, [expected for _, expected in cases])

    def test_singular_and_plural(self):
        js = ("const NOW = Date.UTC(2026,7,16);"
              "console.log(QuipUtils.relativeTime(NOW*1000 - 86400e6, NOW));"
              "console.log(QuipUtils.relativeTime(NOW*1000 - 2*86400e6, NOW));")
        self.assertEqual(self._run(js).splitlines(), ["1 day ago", "2 days ago"])

    def test_future_and_invalid_are_safe(self):
        js = ("const NOW = Date.UTC(2026,7,16);"
              "console.log(JSON.stringify(QuipUtils.relativeTime(NOW*1000 + 9e9, NOW)));"
              "console.log(JSON.stringify(QuipUtils.relativeTime(0, NOW)));"
              "console.log(JSON.stringify(QuipUtils.relativeTime('nope', NOW)));")
        self.assertEqual(self._run(js).splitlines(), ['"just now"', '""', '""'])

    def test_js_and_python_emit_identical_icon_markup(self):
        """The static tree and JS-built search results must not drift apart."""
        from quiport.render.templates import ICON_BY_KIND, icon_use
        kinds = sorted(ICON_BY_KIND)
        js = "".join(f"console.log(QuipUtils.iconHtml({k!r}));" for k in kinds)
        self.assertEqual(self._run(js).splitlines(), [icon_use(k) for k in kinds])

    def test_js_unknown_kind_matches_python_fallback(self):
        from quiport.render.templates import icon_use
        got = self._run("console.log(QuipUtils.iconHtml('mystery'));")
        self.assertEqual(got, icon_use("mystery"))

    def test_js_folder_class_matches_python(self):
        """A folder in the search results must look like the same row in the tree."""
        from quiport.reports import entry_row
        js = ("console.log(QuipUtils.entryHtml({t:'F', p:'f/index.html', k:'folder', u:0}));"
              "console.log(QuipUtils.entryHtml({t:'D', p:'d.html', k:'document', u:0}));")
        folder, document = self._run(js).splitlines()
        self.assertIn('class="quip-entry quip-folder"', folder)
        self.assertIn('class="quip-entry quip-folder"',
                      entry_row("F", "f/index.html", "folder", 0))
        self.assertNotIn("quip-folder", document)

    def test_code_text_turns_br_into_newlines(self):
        """Quip stores newlines in a code block as <br>; textContent drops them
        and would run the whole program onto one line."""
        js = """
        function T(tag, text) { return { nodeType: 1, nodeName: tag,
            childNodes: text === undefined ? [] : [{nodeType:3, nodeValue:text}] }; }
        const pre = { nodeType: 1, nodeName: 'PRE', childNodes: [
            {nodeType:3, nodeValue:'const a = 1;'}, T('BR'),
            {nodeType:3, nodeValue:'const b = 2;'}, T('BR')] };
        console.log(JSON.stringify(QuipUtils.codeText(pre)));
        """
        self.assertEqual(self._run(js), '"const a = 1;\\nconst b = 2;\\n"')

    def test_highlighting_is_a_no_op_without_the_library(self):
        """A page opened with the vendored script missing must still work."""
        js = ("global.document.querySelectorAll = () => [];"
              "console.log(String(QuipUtils.highlightCode()));")
        self.assertEqual(self._run(js), "0")

    def test_the_language_subset_beats_the_full_set_on_short_snippets(self):
        """The reason for restricting candidates at all."""
        import subprocess
        hljs = Path(__file__).resolve().parent.parent / "quiport/assets/highlight.min.js"
        if not hljs.exists():
            self.skipTest("highlight.js not vendored")
        out = subprocess.run(
            ["node", "-e", f"""
             const hljs = require({str(hljs)!r});
             const L = {json.dumps(["javascript","typescript","python","sql","json",
                                    "yaml","xml","css","java","bash","shell","ruby",
                                    "go","php","csharp","markdown"])};
             const code = 'console.log("hey")';
             console.log(hljs.highlightAuto(code).language,
                         hljs.highlightAuto(code, L).language);
             """], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        full, subset = out.stdout.split()
        self.assertEqual(subset, "javascript")
        self.assertNotEqual(full, subset, "the subset is what makes this correct")

    def test_entry_html_escapes_titles(self):
        js = ("console.log(QuipUtils.entryHtml("
              "{t:'<script>x</script>', p:'a.html', k:'document', u:0}));")
        out = self._run(js)
        self.assertNotIn("<script>x", out)
        self.assertIn("&lt;script&gt;", out)

    # -- folder collapse -------------------------------------------------
    # Stub DOM: enough to exercise the toggle's selector chain and state.
    DOM_STUB = """
      function El(cls, tag) {
        return { tag: tag||'div', className: cls||'', hidden: false, children: [],
          parentNode: null, attrs: {},
          getAttribute(n) { return n in this.attrs ? this.attrs[n] : null; },
          setAttribute(n,v) { this.attrs[n]=v; },
          closest(sel) { let n=this; const c=sel.replace('.','');
            while(n) { if((n.className||'').split(' ').includes(c)) return n; n=n.parentNode; }
            return null; },
          querySelector() { return this.children.find(c => c.tag==='ul') || null; },
          appendChild(c) { c.parentNode=this; this.children.push(c); return c; } };
      }
      const li=El('','li'), row=El('quip-row'), btn=El('quip-toggle','button'),
            ul=El('quip-tree','ul'), link=El('quip-entry quip-folder','a');
      li.appendChild(row); row.appendChild(btn); row.appendChild(link); li.appendChild(ul);
      btn.setAttribute('aria-expanded','true');
      let prevented = false;
      function fire(target) {
        HANDLER({ target: target, preventDefault(){ prevented=true; },
                  stopPropagation(){} });
      }
    """

    def _run_dom(self, body):
        import subprocess
        harness = f"""
global.window = global;
let HANDLER = null;
global.document = {{ readyState:'complete',
  addEventListener(t,f){{ if(t==='click') HANDLER=f; }},
  querySelectorAll(){{ return []; }} }};
require({str(self.SCRIPT)!r});
{self.DOM_STUB}
{body}
"""
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()

    def test_toggle_collapses_and_expands(self):
        got = self._run_dom("""
          fire(btn); console.log(ul.hidden + ' ' + btn.getAttribute('aria-expanded'));
          fire(btn); console.log(ul.hidden + ' ' + btn.getAttribute('aria-expanded'));
        """)
        self.assertEqual(got, ["true false", "false true"])

    def test_toggle_updates_its_accessible_label(self):
        got = self._run_dom("fire(btn); console.log(btn.getAttribute('aria-label'));")
        self.assertEqual(got, ["Expand folder"])

    def test_clicking_the_folder_link_neither_collapses_nor_blocks_navigation(self):
        got = self._run_dom(
            "fire(link); console.log(ul.hidden + ' ' + prevented);")
        self.assertEqual(got, ["false false"])


class TestOpenEndedDiff(unittest.TestCase):
    """The diff that found the V1/V2 differences in the first place."""

    @staticmethod
    def kinds(v1, v2):
        from quiport.factcheck import _classify_differences
        return [k for k, _ in _classify_differences(v1, v2)]

    def test_identical_html_has_no_differences(self):
        self.assertEqual(self.kinds("<p>same</p>", "<p>same</p>"), [])

    def test_br_style_is_a_known_category(self):
        self.assertEqual(set(self.kinds("<p>a<br/>b</p>", "<p>a<br>b</p>")), {"br_style"})

    def test_runs_of_br_are_still_br_style(self):
        """The diff groups consecutive tokens, so a run must classify as one."""
        self.assertEqual(set(self.kinds("<p>a<br/><br/><br/>b</p>", "<p>a<br><br><br>b</p>")),
                         {"br_style"})

    def test_blob_url_form_is_a_known_category(self):
        self.assertEqual(
            set(self.kinds("<img src='/blob/T/B'>", "<img src='https://quip.com/blob/T/B'>")),
            {"blob_url_form"})

    def test_live_app_name_is_a_known_category(self):
        self.assertEqual(
            set(self.kinds("<summary>Kanban board</summary>", "<summary></summary>")),
            {"live_app_name"})

    def test_a_genuinely_new_difference_is_reported_as_unknown(self):
        """If Quip changes something nobody predicted, this must not stay green."""
        kinds = self.kinds("<p>the original text</p>", "<p>completely different text</p>")
        self.assertIn("unknown", kinds)


class TestVerify(unittest.TestCase):
    def setUp(self):
        from quiport import verify as V
        self.V = V
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _page(self, name, body):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
        return p

    def test_links_check_passes_when_targets_exist(self):
        self._page("sub/target.html", "hi")
        self._page("sub/source.html", '<a href="target.html">go</a>')
        c = self.V.check_links(self.root)
        self.assertTrue(c.ok, c.failures)

    def test_links_check_catches_a_dangling_link(self):
        self._page("sub/source.html", '<a href="missing.html">go</a>')
        c = self.V.check_links(self.root)
        self.assertFalse(c.ok)
        self.assertEqual(len(c.failures), 1)

    def test_links_check_ignores_absolute_and_root_relative(self):
        self._page("sub/source.html",
                   '<a href="https://example.com">x</a>'
                   '<a href="/lightning/r/001/view">sf</a><a href="#frag">f</a>')
        self.assertTrue(self.V.check_links(self.root).ok)

    def _id_map(self, path="sub/doc.html"):
        (self.root / "id-map.json").write_text(json.dumps(
            {"threads": {"T1": {"path": path, "title": "Doc"}}}), encoding="utf-8")

    def test_index_check_passes_when_every_thread_is_linked(self):
        self._id_map()
        self._page("index.html", '<a href="sub/doc.html">Doc</a>')
        self.assertTrue(self.V.check_index_complete(self.root).ok)

    def test_index_check_catches_a_document_missing_from_the_tree(self):
        """regression: a thread no folder listed vanished from the tree while
        still existing on disk and in id-map."""
        self._id_map()
        self._page("index.html", '<a href="sub/other.html">Other</a>')
        c = self.V.check_index_complete(self.root)
        self.assertFalse(c.ok)
        self.assertIn("sub/doc.html", c.failures[0])

    def test_index_check_ignores_the_inlined_search_index(self):
        """The search JSON names every document, so counting it would mask
        exactly the failure this check exists to find."""
        self._id_map()
        self._page("index.html",
                   '<script>const I = [{"p": "sub/doc.html"}];</script>'
                   '<a href="sub/other.html">Other</a>')
        self.assertFalse(self.V.check_index_complete(self.root).ok)

    def test_index_check_handles_escaped_and_encoded_hrefs(self):
        self._id_map(path="Imported from Quip/A & B/doc.html")
        self._page("index.html", '<a href="Imported%20from%20Quip/A &amp; B/doc.html">D</a>')
        self.assertTrue(self.V.check_index_complete(self.root).ok)

    def test_metadata_check_flags_a_page_with_no_thread_id(self):
        self._page("sub/doc.html", "<p>content</p>")
        c = self.V.check_metadata(self.root)
        self.assertFalse(c.ok)

    def test_metadata_check_accepts_a_proper_block(self):
        self._page("sub/doc.html", '<blockquote data-quip-thread-id="T1"></blockquote>')
        self.assertTrue(self.V.check_metadata(self.root).ok)

    def test_reports_check_lists_what_is_missing(self):
        c = self.V.check_reports(self.root)
        self.assertFalse(c.ok)
        self.assertIn("index.html", c.failures)




class TestSecurityPosture(unittest.TestCase):
    """Properties that protect the credential and the reader."""

    def test_a_bearer_token_is_never_sent_over_http(self):
        from quiport.quipclient import QuipClient
        QuipClient("t")                                   # default is https
        QuipClient("t", "https://acme.example.com")       # VPC, still https
        for bad in ("http://platform.quip.com", "platform.quip.com",
                    "HTTP://platform.quip.com", ""):
            with self.subTest(base_url=bad):
                with self.assertRaises(ValueError):
                    QuipClient("t", bad)

    def test_the_token_travels_only_as_a_header(self):
        """Never in a URL, where it would reach logs and referrers."""
        import inspect
        from quiport import quipclient as q
        source = inspect.getsource(q.QuipClient._fetch)
        self.assertIn('"Authorization": "Bearer " + self.token', source)
        url_build = source[source.index("url = "):source.index("attempt = 0")]
        self.assertNotIn("token", url_build)

    def test_request_headers_are_never_persisted(self):
        """The cache records response headers; recording ours would store the
        credential on disk forever."""
        import inspect
        from quiport.rawstore import RawStore
        self.assertNotIn("request_headers", inspect.getsource(RawStore.save))

    def test_offline_check_catches_a_remote_resource(self):
        from quiport import verify as V
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        try:
            (root / "a.html").write_text(
                '<img src="../local.png"><a href="https://example.com">ok</a>',
                encoding="utf-8")
            self.assertTrue(V.check_offline(root).ok, "local src and links are fine")
            (root / "b.html").write_text(
                '<img src="https://tracker.example/pixel.gif">', encoding="utf-8")
            check = V.check_offline(root)
            self.assertFalse(check.ok)
            self.assertIn("b.html", check.failures[0])
        finally:
            tmp.cleanup()


class TestMissingCommentFiles(unittest.TestCase):
    """A comment's attachment that could not be fetched is reported, not just shown."""

    def _message(self):
        return Message.from_raw({"id": "m1", "text": "see attached", "created_usec": 1,
                                 "author_name": "Alex", "visible": True,
                                 "files": [{"hash": "B1", "name": "plan.pdf"},
                                           {"hash": "B2", "name": "photo.png"}]})

    def test_a_miss_is_marked_inline_and_recorded(self):
        report = RenderReport("T1", "Doc")
        found = {"B2": "Doc_files/photo.png"}
        resolver = C.recording_resolver(
            lambda _t, blob_id, _e, _p, name_hint="": found.get(blob_id), report)
        html = C._message_html(self._message(), "Doc.html", resolver)
        self.assertIn("[missing file: plan.pdf]", html)
        self.assertIn('src="Doc_files/photo.png"', html)
        self.assertEqual(report.count("missing_comment_file"), 1)
        self.assertEqual(report.entries[0]["label"], "plan.pdf")

    def test_it_appears_under_content_loss(self):
        import inspect
        from quiport import reports
        src = inspect.getsource(reports.import_notes)
        self.assertIn('"missing_comment_file"', src)
        self.assertIn("in comments", src)

    def test_documents_and_chats_both_record(self):
        import inspect
        from quiport.render import chat, document
        for fn in (chat.render_chat, document.render_document):
            self.assertIn("recording_resolver", inspect.getsource(fn), fn.__name__)


class TestEveryPageHasTheSearchBar(unittest.TestCase):
    """Documents, folders, chats and shortcuts all carry the same nav and search,
    plus the icons search results are drawn with."""

    def test_shortcut_pages(self):
        from quiport.model import Thread
        from quiport.render.document import render_stub
        ctx = type("Ctx", (), {"exported_at": staticmethod(lambda _id: None)})()
        html = render_stub(Thread(id="DocAAAxmpl1", title="Doc"),
                           "Imported from Quip/A/B/x.html", "Imported from Quip/x.html", ctx)
        self.assertIn('<nav class="quip-nav">', html)
        self.assertIn('href="../../../import-notes.html"', html)
        self.assertIn('src="../../../quip-utils.js"', html)
        for kind in ("folder", "document", "spreadsheet", "slides", "chat"):
            self.assertIn(f'id="quip-icon-{kind}"', html)

    def test_chat_and_document_pages(self):
        import inspect
        from quiport.render import chat, document
        for fn in (chat.render_chat, document.render_document):
            src = inspect.getsource(fn)
            self.assertIn("site_chrome(page_path)", src, fn.__name__)
            self.assertIn("ICON_BY_KIND", src, fn.__name__)


class TestAuditNextSteps(unittest.TestCase):
    """A failed audit ends with the exact commands to run, for this export."""

    def _result(self, **over):
        from quiport.audit import ThreadAudit
        t = ThreadAudit("DocAAAxmpl1", "Doc")
        t.gap("html", "cursor chain stopped", refetch=("/2/threads/DocAAAxmpl1/html", {}))
        r = {"threads": 1, "complete": 0, "incomplete": 1, "refused": 0,
             "results": [t], "gaps_by_kind": {"html": 1}, "discovery_gaps": [],
             "folder_gaps": [], "non_answers": [],
             "index": {"missing_from_index_count": 0}, "failed_attempts": 0}
        r.update(over)
        return r

    def _next(self, result, export_id="acme"):
        from quiport.audit import report
        text = report(result, export_id)
        return text[text.index("## What to do next"):]

    def test_steps_come_last_and_use_the_real_export_id(self):
        from quiport.audit import report
        text = report(self._result(), "acme-team")
        self.assertTrue(text.rstrip().endswith("verify --export-id acme-team"))
        self.assertIn("audit --export-id acme-team --repair --token", text)
        self.assertNotIn("myaccount", text)

    def test_an_incomplete_thread_needs_repair_rebuild_audit_verify(self):
        steps = self._next(self._result())
        order = [steps.index(s) for s in ("audit --export-id acme --repair",
                                          "all --export-id acme --token",
                                          "audit --export-id acme\n",
                                          "verify --export-id acme")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("reindex", steps)
        self.assertNotIn("--rediscover", steps)

    def test_each_problem_brings_its_own_fix(self):
        steps = self._next(self._result(
            incomplete=0, results=[],
            discovery_gaps=[("forgotten", "DocAAAxmpl2 was harvested but is gone")],
            folder_gaps=[("folder", "FldAOAxmpl2 never fetched")],
            index={"missing_from_index_count": 3}))
        self.assertIn("reindex --export-id acme", steps)
        self.assertIn("all --export-id acme --rediscover", steps)
        self.assertIn("refresh --export-id acme DocAAAxmpl2 --token", steps)
        self.assertNotIn("--repair", steps, "no thread is missing anything")
        self.assertLess(steps.index("reindex"), steps.index("--rediscover"))

    def test_a_clean_audit_points_to_verify(self):
        steps = self._next(self._result(incomplete=0, complete=1, results=[]))
        self.assertIn("Nothing to fix", steps)
        self.assertIn("verify --export-id acme", steps)
        self.assertNotIn("--token", steps)

    def test_an_export_id_with_a_space_is_quoted(self):
        self.assertIn("--export-id 'my account'", self._next(self._result(), "my account"))


class TestSearchRanking(unittest.TestCase):
    """Title search: exact first, then every word, then fuzzy -- one list."""

    SCRIPT = TestBrowserUtils.SCRIPT
    setUp = TestBrowserUtils.setUp
    _run = TestBrowserUtils._run

    TITLES = ["Fun Recipe Ideas", "Fun Ideas", "Fun Ideas 2024", "More Fun Ideas",
              "Ideas for Fun", "Café Q3-Notes", "Infrastructure Design"]

    def _ranked(self, query, titles=None):
        """[[title, tier, highlighted]] best first, as the search box sorts them."""
        return json.loads(self._run(f"""
const U = window.QuipUtils, q = {json.dumps(query)};
const rows = {json.dumps(titles or self.TITLES)}
  .map(t => ({{t, m: U.matchTitle(t, q)}})).filter(r => r.m)
  .sort((a, b) => (a.m.tier - b.m.tier) || (a.m.rank - b.m.rank) ||
                  (a.t.length - b.t.length));
console.log(JSON.stringify(rows.map(r => [r.t, r.m.tier,
  U.highlightTitle(r.t, r.m.positions)])));
"""))

    def test_words_need_not_be_adjacent(self):
        titles = [r[0] for r in self._ranked("Fun Ideas")]
        self.assertIn("Fun Recipe Ideas", titles)

    def test_exact_matches_rank_above_word_matches(self):
        ranked = self._ranked("Fun Ideas")
        self.assertEqual(ranked[0][0], "Fun Ideas", "the title itself first")
        tiers = [r[1] for r in ranked]
        self.assertEqual(tiers, sorted(tiers))
        self.assertLess([r[0] for r in ranked].index("More Fun Ideas"),
                        [r[0] for r in ranked].index("Fun Recipe Ideas"))

    def test_typed_order_beats_reordered_words(self):
        titles = [r[0] for r in self._ranked("fun ideas")]
        self.assertLess(titles.index("Fun Recipe Ideas"), titles.index("Ideas for Fun"))

    def test_fuzzy_letters_match_in_order(self):
        ranked = self._ranked("frcp")
        self.assertEqual([r[0] for r in ranked], ["Fun Recipe Ideas"])
        self.assertEqual(ranked[0][1], 2)

    def test_fuzzy_words_must_start_a_word(self):
        """Without this, a long title loosely matches any short query."""
        # "f" and "d" both occur in order in "Infrastructure Design", but the
        # "f" is mid-word.
        self.assertEqual(self._ranked("fd", ["Infrastructure Design"]), [])
        self.assertEqual(self._ranked("id", ["Infrastructure Design"])[0][1], 2)
        self.assertEqual(self._ranked("zzqx"), [])

    def test_accents_and_separators_are_ignored(self):
        ranked = self._ranked("cafe q3 notes")
        self.assertEqual(ranked[0][0], "Café Q3-Notes")
        self.assertEqual(ranked[0][1], 0)

    def test_matched_letters_are_highlighted(self):
        row = dict((r[0], r[2]) for r in self._ranked("fun ideas"))
        self.assertEqual(row["Fun Recipe Ideas"],
                         '<mark class="quip-hit">Fun</mark> Recipe '
                         '<mark class="quip-hit">Ideas</mark>')
        # Accent-folded matches highlight the original characters.
        self.assertIn('<mark class="quip-hit">Café</mark>',
                      self._ranked("cafe")[0][2])

    def test_highlighting_escapes_the_title(self):
        out = self._ranked("b", ["<b>bold</b>"])
        self.assertNotIn("<b>", out[0][2])
        self.assertIn("&lt;", out[0][2])

    def test_search_box_ranks_folders_with_documents_and_marks_hits(self):
        index = [
            {"t": "Ideas for Fun", "p": "a.html", "k": "document", "u": 1},
            {"t": "Fun Ideas", "p": "Imported from Quip/Fun Ideas/index.html",
             "f": "Imported from Quip/Fun Ideas", "k": "folder", "c": "blue", "u": 1},
            {"t": "Fun Recipe Ideas", "p": "b.html", "k": "document", "u": 1},
            {"t": "Unrelated", "p": "Imported from Quip/Fun Ideas/c.html",
             "f": "Imported from Quip/Fun Ideas", "k": "document", "u": 1},
        ]
        html = self._run(f"""
window.QUIP_INDEX = {json.dumps(index)};
function el(a) {{ return {{ attrs:a||{{}}, hidden:false, value:'', innerHTML:'',
  style:{{}}, L:{{}}, querySelectorAll(){{ return []; }},
  addEventListener(t,f){{ (this.L[t]=this.L[t]||[]).push(f); }},
  getAttribute(n){{ return this.attrs[n]; }},
  fire(t){{ (this.L[t]||[]).forEach(function(f){{ f(); }}); }} }}; }}
var box=el(), out=el(), nav=el({{href:'index.html'}});
var scope={{ querySelector:function(s){{ return {{'#quip-q':box,'#quip-results':out,
  '.quip-nav a':nav}}[s] || null; }}, querySelectorAll:function(){{ return []; }} }};
window.QuipUtils.initSiteSearch(scope);
box.value='Fun Ideas'; box.fire('input');
console.log(out.innerHTML);
""")
        self.assertIn("3 matches", html)
        order = [html.index(t) for t in ('href="Imported%20from%20Quip/Fun%20Ideas/index.html"',
                                         'href="b.html"', 'href="a.html"')]
        self.assertEqual(order, sorted(order), "folder, then word match, then reordered")
        self.assertNotIn("c.html", html, "a folder's name no longer matches its contents")
        self.assertIn('<mark class="quip-hit">', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
