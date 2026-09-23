import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from preview import (
    Api,
    FileChangeHandler,
    PreviewApp,
    SaveConflictError,
    atomic_export_utf8,
    atomic_write_utf8,
    content_hash,
    watched_file_signature,
)


class AtomicSaveTests(unittest.TestCase):
    def test_atomic_save_checks_hash_and_preserves_mode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o640)

            new_hash = atomic_write_utf8(path, "new", content_hash("old"))

            self.assertEqual(path.read_text(encoding="utf-8"), "new")
            self.assertEqual(new_hash, content_hash("new"))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_conflict_rejects_without_writing_and_force_overwrites(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("disk", encoding="utf-8")

            with self.assertRaises(SaveConflictError) as context:
                atomic_write_utf8(path, "editor", content_hash("stale"))
            self.assertEqual(context.exception.disk_hash, content_hash("disk"))
            self.assertEqual(path.read_text(encoding="utf-8"), "disk")

            atomic_write_utf8(path, "editor", content_hash("stale"), force=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "editor")

    def test_replace_failure_keeps_original_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "note.md"
            path.write_text("old", encoding="utf-8")

            def fail_replace(source, destination):
                raise OSError("simulated replace failure")

            with self.assertRaises(OSError):
                atomic_write_utf8(path, "new", content_hash("old"), replace=fail_replace)

            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(root.glob(".note.md.*.tmp")), [])

    def test_deleted_file_is_conflict_unless_force_recreates_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("old", encoding="utf-8")
            expected = content_hash("old")
            path.unlink()
            with self.assertRaises(SaveConflictError):
                atomic_write_utf8(path, "new", expected)
            atomic_write_utf8(path, "new", expected, force=True)
            self.assertEqual(path.read_text(encoding="utf-8"), "new")


class FakeWindow:
    def __init__(self, dialog_result=None, confirmation_result=False):
        self.dialog_result = dialog_result
        self.confirmation_result = confirmation_result
        self.confirmations = []
        self.scripts = []

    def create_file_dialog(self, *args, **kwargs):
        return self.dialog_result

    def evaluate_js(self, script):
        self.scripts.append(script)

    def create_confirmation_dialog(self, title, message):
        self.confirmations.append((title, message))
        return self.confirmation_result


class ApiSaveTests(unittest.TestCase):
    def make_app(self, path, content):
        app = PreviewApp()
        normalized = app._normalize_path(path)
        app._watchers[normalized] = {
            "observer": None,
            "hash": content_hash(content),
            "token": 1,
        }
        return app, normalized

    def test_api_returns_structured_conflict_then_allows_force(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("loaded", encoding="utf-8")
            app, normalized = self.make_app(path, "loaded")
            path.write_text("external", encoding="utf-8")

            conflict = app._api.save_file(normalized, "editor", content_hash("loaded"), False)
            self.assertFalse(conflict["ok"])
            self.assertTrue(conflict["conflict"])
            self.assertEqual(path.read_text(encoding="utf-8"), "external")

            saved = app._api.save_file(normalized, "editor", conflict["disk_hash"], True)
            self.assertTrue(saved["ok"])
            self.assertEqual(saved["hash"], content_hash("editor"))
            self.assertEqual(app._watchers[normalized]["hash"], saved["hash"])

    def test_reopen_keeps_existing_watcher_and_sends_hash(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")
            watcher = app._watchers[normalized]
            app._window = FakeWindow()
            path.write_text("reopened", encoding="utf-8")

            app.load_file(path)

            self.assertIs(app._watchers[normalized], watcher)
            self.assertEqual(watcher["hash"], content_hash("reopened"))
            self.assertIn(content_hash("reopened"), app._window.scripts[0])
            self.assertIn("window.addTabFromPython", app._window.scripts[0])

    def test_stale_watcher_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("changed", encoding="utf-8")
            app, normalized = self.make_app(path, "old")
            app._window = FakeWindow()

            app._watchers[normalized]["token"] = 2
            app._on_file_changed(normalized, 1)
            app._apply_file_change(normalized, 1)

            self.assertEqual(app._window.scripts, [])
            self.assertIsNone(app._watchers[normalized].get("debounce"))

    def test_file_change_is_debounced_and_reloaded_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")
            app._window = FakeWindow()
            token = app._watchers[normalized]["token"]

            app._on_file_changed(normalized, token)
            first_timer = app._watchers[normalized]["debounce"]
            self.assertIsNotNone(first_timer)
            app._on_file_changed(normalized, token)
            self.assertIsNot(first_timer, app._watchers[normalized]["debounce"])
            # cancel() sets the timer's finished event; is_alive() is racy because
            # the cancelled thread may still be winding down.
            self.assertTrue(first_timer.finished.is_set())
            app._watchers[normalized]["debounce"].cancel()

            path.write_text("second", encoding="utf-8")
            app._apply_file_change(normalized, token)

            self.assertEqual(len(app._window.scripts), 1)
            self.assertIn("window.updateTabFromPython", app._window.scripts[0])
            self.assertIn("second", app._window.scripts[0])
            self.assertEqual(app._watchers[normalized]["hash"], content_hash("second"))


class ReloadFileTests(unittest.TestCase):
    def make_app(self, path, content):
        app = PreviewApp()
        normalized = app._normalize_path(path)
        app._watchers[normalized] = {
            "observer": None,
            "hash": content_hash(content),
            "signature": watched_file_signature(normalized),
            "token": 1,
            "debounce": None,
        }
        return app, normalized

    def test_reload_returns_disk_content_and_refreshes_watcher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")
            path.write_text("second", encoding="utf-8")

            response = app._api.reload_file(normalized)

            self.assertTrue(response["ok"])
            self.assertEqual(response["content"], "second")
            self.assertEqual(response["hash"], content_hash("second"))
            self.assertEqual(app._watchers[normalized]["hash"], content_hash("second"))

    def test_reload_reports_a_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")
            path.unlink()

            response = app._api.reload_file(normalized)

            self.assertFalse(response["ok"])
            self.assertTrue(response["missing"])

    def test_reload_rejects_a_path_that_is_not_open(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app = PreviewApp()

            response = app._api.reload_file(app._normalize_path(path))

            self.assertFalse(response["ok"])
            self.assertIn("not open", response["error"])


class WatchPollTests(unittest.TestCase):
    def make_app(self, path, content):
        app = PreviewApp()
        app._window = FakeWindow()
        normalized = app._normalize_path(path)
        app._watchers[normalized] = {
            "observer": None,
            "hash": content_hash(content),
            "signature": watched_file_signature(normalized),
            "token": 1,
            "debounce": None,
        }
        return app, normalized

    def test_poll_triggers_only_when_the_file_signature_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")

            app._check_watched_files_once()
            self.assertIsNone(app._watchers[normalized]["debounce"])

            path.write_text("second revision", encoding="utf-8")
            app._check_watched_files_once()

            timer = app._watchers[normalized]["debounce"]
            self.assertIsNotNone(timer)
            timer.cancel()

    def test_poll_picks_up_an_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "note.md"
            path.write_text("first", encoding="utf-8")
            app, normalized = self.make_app(path, "first")
            temporary_path = root / ".note.md.agent.tmp"
            temporary_path.write_text("agent update", encoding="utf-8")
            os.replace(temporary_path, path)

            app._check_watched_files_once()
            app._watchers[normalized]["debounce"].cancel()
            app._apply_file_change(normalized, 1)

            self.assertEqual(len(app._window.scripts), 1)
            self.assertIn("window.updateTabFromPython", app._window.scripts[0])
            self.assertIn("agent update", app._window.scripts[0])
            self.assertEqual(app._watchers[normalized]["hash"], content_hash("agent update"))


class FileChangeHandlerTests(unittest.TestCase):
    def test_atomic_replace_destination_is_forwarded(self):
        calls = []
        handler = FileChangeHandler("/tmp/example-note.md", 3, lambda path, token: calls.append((path, token)))

        handler.on_moved(SimpleNamespace(is_directory=False, src_path="/tmp/.example-note.md.tmp", dest_path="/tmp/example-note.md"))

        self.assertEqual(calls, [("/tmp/example-note.md", 3)])

    def test_unrelated_and_directory_events_are_ignored(self):
        calls = []
        handler = FileChangeHandler("/tmp/example-note.md", 3, lambda path, token: calls.append((path, token)))

        handler.on_modified(SimpleNamespace(is_directory=False, src_path="/tmp/other.md"))
        handler.on_created(SimpleNamespace(is_directory=True, src_path="/tmp"))

        self.assertEqual(calls, [])


class ApiExportTests(unittest.TestCase):
    def make_api(self, dialog_result):
        app = type("FakeApp", (), {})()
        app._window = FakeWindow(dialog_result)
        return Api(app)

    def test_export_cancellation(self):
        self.assertTrue(self.make_api(None).export_html("Note", "<p>x</p>", "")["cancelled"])
        self.assertTrue(self.make_api([]).export_html("Note", "<p>x</p>", "")["cancelled"])

    def test_export_accepts_list_and_string_dialog_results(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            list_path = Path(temporary_directory) / "list.html"
            string_path = Path(temporary_directory) / "string.html"

            self.assertTrue(self.make_api([str(list_path)]).export_html("List", "<p>list</p>", "")["ok"])
            self.assertTrue(self.make_api(str(string_path)).export_html("String", "<p>string</p>", "")["ok"])
            self.assertIn("<p>list</p>", list_path.read_text(encoding="utf-8"))
            self.assertIn("<p>string</p>", string_path.read_text(encoding="utf-8"))

    def test_export_write_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            response = self.make_api(temporary_directory).export_html("Bad", "<p>x</p>", "")
            self.assertFalse(response["ok"])
            self.assertIn("Export failed", response["error"])

    def test_atomic_export_replace_failure_preserves_old_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "existing.html"
            path.write_text("old export", encoding="utf-8")

            def fail_replace(source, destination):
                raise OSError("simulated export replace failure")

            with self.assertRaises(OSError):
                atomic_export_utf8(path, "new export", replace=fail_replace)

            self.assertEqual(path.read_text(encoding="utf-8"), "old export")
            self.assertEqual(list(root.glob(".existing.html.*.tmp")), [])


class NativeDirtyGuardTests(unittest.TestCase):
    def test_bridge_updates_python_dirty_state_narrowly(self):
        app = PreviewApp()
        self.assertFalse(app._has_unsaved_changes)
        self.assertTrue(app._api.set_dirty_state(True)["ok"])
        self.assertTrue(app._has_unsaved_changes)
        app._api.set_dirty_state(False)
        self.assertFalse(app._has_unsaved_changes)
        app._api.set_dirty_state(1)
        self.assertFalse(app._has_unsaved_changes)

    def test_clean_close_allows_without_confirmation(self):
        app = PreviewApp()
        app._window = FakeWindow(confirmation_result=False)

        self.assertTrue(app._on_closing())
        self.assertEqual(app._window.confirmations, [])

    def test_dirty_close_honors_cancel_and_confirm(self):
        app = PreviewApp()
        app._has_unsaved_changes = True
        app._window = FakeWindow(confirmation_result=False)
        self.assertFalse(app._on_closing())
        self.assertEqual(len(app._window.confirmations), 1)
        self.assertIn("unsaved", app._window.confirmations[0][1].lower())

        app._window = FakeWindow(confirmation_result=True)
        self.assertTrue(app._on_closing())
        self.assertEqual(len(app._window.confirmations), 1)


class ExternalUrlTests(unittest.TestCase):
    def setUp(self):
        app = type("FakeApp", (), {})()
        app._window = FakeWindow()
        self.api = Api(app)

    def test_only_narrow_external_schemes_are_opened(self):
        with mock.patch("preview.webbrowser.open", return_value=True) as opener:
            self.assertTrue(self.api.open_external_url("https://example.com/path")["ok"])
            opener.assert_called_once_with("https://example.com/path")

        for unsafe in ("javascript:alert(1)", "https:\n//example.com", "file:///tmp/note"):
            with mock.patch("preview.webbrowser.open") as opener:
                self.assertFalse(self.api.open_external_url(unsafe)["ok"])
                opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
