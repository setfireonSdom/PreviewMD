"""Unsaved drafts must survive a restart, because a draft exists nowhere else.

A file-backed tab is autosaved within a second, so the window in which a crash
costs work is about a second. A tab with no path has no such safety: it lives
only in the page, and before this the session writer skipped it outright
(`if (!tab.path) continue`), so a quit or a crash destroyed it outright. The only
thing standing in the way was a confirmation dialog, which a crash never shows.
"""

import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

import preview
from preview import build_html, normalize_session


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)

    def set_title(self, title):
        pass


class DraftNormalisationTests(unittest.TestCase):
    def test_a_draft_survives_the_session_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            saved = {
                "version": preview.SESSION_VERSION,
                "tabs": [],
                "drafts": [{"name": "notes.md", "content": "还没保存的想法", "preview_ratio": 0.25}],
            }
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            session = preview.load_session(path)
            self.assertEqual(len(session["drafts"]), 1)
            self.assertEqual(session["drafts"][0]["content"], "还没保存的想法")
            self.assertEqual(session["drafts"][0]["preview_ratio"], 0.25)

    def test_unusable_drafts_are_dropped(self):
        session = normalize_session(
            {
                "drafts": [
                    "not a dict",
                    {"name": "a.md"},
                    {"name": "b.md", "content": ""},
                    {"name": "c.md", "content": None},
                ]
            }
        )
        self.assertEqual(session["drafts"], [])

    def test_an_oversized_draft_is_skipped_rather_than_written(self):
        # At that size the text belongs in a file, and the session file is not a
        # document store. It must be dropped, not truncated: half a document is
        # worse than none, because the user would not know it was incomplete.
        oversized = "x" * (preview.MAX_SESSION_DRAFT_CHARS + 1)
        session = normalize_session({"drafts": [{"name": "big.md", "content": oversized}]})
        self.assertEqual(session["drafts"], [])

    def test_a_draft_just_under_the_limit_is_kept(self):
        content = "x" * preview.MAX_SESSION_DRAFT_CHARS
        session = normalize_session({"drafts": [{"name": "big.md", "content": content}]})
        self.assertEqual(len(session["drafts"]), 1)

    def test_a_draft_name_is_always_openable(self):
        session = normalize_session(
            {"drafts": [{"name": "  ", "content": "x"}, {"name": "notes", "content": "y"}]}
        )
        self.assertEqual(session["drafts"][0]["name"], "Untitled.md")
        self.assertEqual(session["drafts"][1]["name"], "notes.md")

    def test_scroll_ratios_are_clamped(self):
        session = normalize_session(
            {
                "drafts": [
                    {"name": "a.md", "content": "x", "preview_ratio": 5},
                    {"name": "b.md", "content": "y", "preview_ratio": -1},
                    {"name": "c.md", "content": "z", "preview_ratio": "half"},
                ]
            }
        )
        self.assertEqual([d["preview_ratio"] for d in session["drafts"]], [1.0, 0.0, 0.0])

    def test_the_active_draft_must_actually_exist(self):
        session = normalize_session(
            {"drafts": [{"name": "a.md", "content": "x"}], "active_draft": "gone.md"}
        )
        self.assertIsNone(session["active_draft"])
        session = normalize_session(
            {"drafts": [{"name": "a.md", "content": "x"}], "active_draft": "a.md"}
        )
        self.assertEqual(session["active_draft"], "a.md")

    def test_an_old_session_without_drafts_still_loads(self):
        session = normalize_session({"tabs": ["/tmp/a.md"], "active": 0, "view_mode": "split"})
        self.assertEqual(session["drafts"], [])
        self.assertIsNone(session["active_draft"])
        self.assertEqual(session["view_mode"], "split")


class DraftPageWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_the_page_sends_drafts_in_the_session(self):
        # The bug this guards: `if (!tab.path) continue` dropped every draft.
        self.assertNotIn("        if (!tab.path) continue;\n        if (i <= activeIdx)", self.document)
        self.assertIn("drafts: drafts,", self.document)
        self.assertIn("active_draft: activeDraft,", self.document)

    def test_the_page_reopens_drafts(self):
        self.assertIn("if (Array.isArray(state.drafts)) {", self.document)
        self.assertIn("addTab(draft.name || 'Untitled.md', null, draft.content, null);", self.document)

    def test_an_active_draft_becomes_the_active_tab(self):
        self.assertIn("if (draft.name === state.active_draft) draftTab = tab;", self.document)
        self.assertIn("if (draftTab) {", self.document)
        self.assertIn("activeIdx = tabs.indexOf(draftTab);", self.document)

    def test_python_passes_drafts_to_the_page(self):
        # These live in the Python source, not in the generated page.
        source = inspect.getsource(preview)
        self.assertIn('"drafts": self._session["drafts"],', source)
        self.assertIn('"active_draft": self._session["active_draft"],', source)
        self.assertIn("Restoring %d unsaved draft(s)", source)

    def test_draft_text_never_reaches_the_log(self):
        # A draft is document text. Restoring one must describe it by count only.
        with tempfile.TemporaryDirectory() as directory:
            log_dir = os.path.join(directory, "logs")
            preview.configure_logging(log_dir)
            self.addCleanup(preview.configure_logging, tempfile.mkdtemp())

            secret = "这一行是草稿正文，不应该出现在日志里"
            session_path = Path(directory) / "session.json"
            session_path.write_text(
                json.dumps(
                    {
                        "version": preview.SESSION_VERSION,
                        "tabs": [],
                        "drafts": [{"name": "notes.md", "content": secret}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            app = preview.PreviewApp(session_path=session_path)
            app._window = FakeWindow()
            app._restore_session()

            with open(os.path.join(log_dir, "previewmd.log"), encoding="utf-8") as handle:
                logged = handle.read()
            self.assertIn("Restoring 1 unsaved draft(s)", logged)
            self.assertNotIn(secret, logged)


if __name__ == "__main__":
    unittest.main()
