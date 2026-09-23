import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from preview import (
    Api,
    PreviewApp,
    content_hash,
    display_path,
    ensure_document_extension,
    load_session,
    normalize_session,
    persist_session,
    plan_session_restore,
    safe_save_filename,
    session_file_path,
)


class FakeElement:
    def __init__(self):
        self.listeners = []

    def on(self, event, callback):
        self.listeners.append((event, callback))


class FakeDom:
    def __init__(self):
        self.body = FakeElement()

    def get_element(self, selector):
        return self.body if selector == "body" else None


class FakeWindow:
    def __init__(self, dialog_result=None, dom=None):
        self.dialog_result = dialog_result
        self.dom = dom or FakeDom()
        self.scripts = []
        self.dialogs = []

    def create_file_dialog(self, *args, **kwargs):
        self.dialogs.append((args, kwargs))
        return self.dialog_result

    def evaluate_js(self, script):
        self.scripts.append(script)


class SessionFileTests(unittest.TestCase):
    def test_env_override_wins_and_default_follows_platform(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            override = str(Path(temporary_directory) / "session.json")
            self.assertEqual(
                session_file_path({"PREVIEWMD_SESSION_FILE": override}),
                str(Path(override).resolve()),
            )

        with mock.patch("preview.sys.platform", "darwin"):
            default = session_file_path({})
        self.assertTrue(default.endswith("Library/Application Support/PreviewMD/session.json"))

    def test_normalize_session_drops_unopenable_entries(self):
        session = normalize_session({
            "tabs": ["./a.md", "b.txt", "c.pdf", "a.md", 7, ""],
            "active": "3",
            "view_mode": "split",
        })
        self.assertEqual(
            [Path(path).name for path in session["tabs"]],
            ["a.md", "b.txt"],
        )
        self.assertTrue(all(Path(path).is_absolute() for path in session["tabs"]))
        self.assertEqual(session["active"], 3)
        self.assertEqual(session["view_mode"], "split")

    def test_normalize_session_rejects_broken_payloads(self):
        for payload in (None, "text", 42, {"tabs": "a.md"}, {"tabs": [None]}):
            session = normalize_session(payload)
            self.assertEqual(session["tabs"], [])
            self.assertEqual(session["active"], 0)
            self.assertEqual(session["view_mode"], "preview")
            self.assertEqual(session["last_directory"], "")

    def test_normalize_session_keeps_only_usable_last_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            expected = str(Path(temporary_directory).resolve())
            session = normalize_session({"last_directory": temporary_directory})
            self.assertEqual(session["last_directory"], expected)

        for broken in ("", "   ", 7, None, {"path": "/tmp"}):
            self.assertEqual(normalize_session({"last_directory": broken})["last_directory"], "")

    def test_persist_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state" / "session.json"
            directory = str(Path(temporary_directory).resolve())
            self.assertTrue(persist_session(
                {
                    "tabs": [str(Path(temporary_directory) / "note.md")],
                    "active": 0,
                    "view_mode": "edit",
                    "last_directory": directory,
                },
                path,
            ))
            self.assertTrue(path.is_file())
            loaded = load_session(path)
            self.assertEqual(loaded["view_mode"], "edit")
            self.assertEqual(loaded["last_directory"], directory)

    def test_corrupt_or_unwritable_session_degrades_safely(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_session(path)["tabs"], [])
            self.assertFalse(persist_session({"tabs": []}, temporary_directory))

    def test_restore_plan_drops_missing_files_and_remaps_active(self):
        existing = {"/tmp/one.md": True, "/tmp/three.md": True}

        def exists(path):
            return existing.get(path, False)

        restored, active = plan_session_restore(
            ["/tmp/one.md", "/tmp/two.md", "/tmp/three.md"], 2, exists
        )
        self.assertEqual(restored, ["/tmp/one.md", "/tmp/three.md"])
        self.assertEqual(active, 1)

        self.assertEqual(plan_session_restore(["/tmp/gone.md"], 0, exists), ([], 0))


class SessionBridgeTests(unittest.TestCase):
    def test_api_persists_session_to_configured_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.json"
            app = PreviewApp(session_path=str(path))
            response = app._api.save_session({"tabs": [], "active": 0, "view_mode": "preview"})
            self.assertTrue(response["ok"])
            self.assertIn("tabs", json.loads(path.read_text(encoding="utf-8")))

    def test_api_save_session_injects_the_last_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session.json"
            directory = str(Path(temporary_directory).resolve())
            app = PreviewApp(session_path=str(path))
            app._last_directory = directory
            self.assertTrue(app._api.save_session({"tabs": []})["ok"])
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["last_directory"],
                directory,
            )

    def test_dialog_directory_ignores_a_vanished_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = PreviewApp(session_path=str(Path(temporary_directory) / "session.json"))
            self.assertEqual(app._dialog_directory(), "")
            app._last_directory = str(Path(temporary_directory).resolve())
            self.assertEqual(app._dialog_directory(), app._last_directory)
            app._last_directory = str(Path(temporary_directory) / "gone")
            self.assertEqual(app._dialog_directory(), "")

    def test_open_file_dialog_starts_in_the_last_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = PreviewApp(session_path=str(Path(temporary_directory) / "session.json"))
            app._last_directory = str(Path(temporary_directory).resolve())
            app._window = FakeWindow(None)
            app._api.open_file()
            _, kwargs = app._window.dialogs[-1]
            self.assertEqual(kwargs["directory"], app._last_directory)

    def test_restore_reopens_saved_tabs_and_reports_active_tab(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            first = Path(temporary_directory) / "first.md"
            second = Path(temporary_directory) / "second.md"
            missing = Path(temporary_directory) / "missing.md"
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            session_path = Path(temporary_directory) / "session.json"
            session_path.write_text(json.dumps({
                "tabs": [str(first), str(missing), str(second)],
                "active": 2,
                "view_mode": "split",
            }), encoding="utf-8")

            app = PreviewApp(session_path=str(session_path))
            window = FakeWindow()
            app._window = window
            loaded = []
            with mock.patch.object(app, "load_file", side_effect=loaded.append):
                app._restore_session()

            self.assertEqual(loaded, [str(first.resolve()), str(second.resolve())])
            self.assertIn("finishSessionRestore", window.scripts[0])
            self.assertIn('"active": 1', window.scripts[0])
            self.assertIn('"view_mode": "split"', window.scripts[0])


class NativeDropTests(unittest.TestCase):
    def test_native_drop_opens_markdown_paths_and_reports_others(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            note = Path(temporary_directory) / "note.md"
            note.write_text("body", encoding="utf-8")
            image = Path(temporary_directory) / "photo.png"
            image.write_bytes(b"png")

            app = PreviewApp(session_path=str(Path(temporary_directory) / "session.json"))
            app._window = FakeWindow()
            loaded = []
            with mock.patch.object(app, "load_file", side_effect=loaded.append):
                app.handle_native_drop({"dataTransfer": {"files": [
                    {"pywebviewFullPath": str(note)},
                    {"pywebviewFullPath": str(image)},
                    {"name": "without-path.md"},
                ]}})

            self.assertEqual(loaded, [str(note)])
            self.assertEqual(app._window.scripts, [])

            with mock.patch.object(app, "load_file") as loader:
                app.handle_native_drop({"dataTransfer": {"files": [{"pywebviewFullPath": str(image)}]}})
            loader.assert_not_called()
            self.assertIn("showStatus", app._window.scripts[-1])

    def test_register_native_drop_marks_the_page_ready(self):
        app = PreviewApp(session_path="/tmp/previewmd-test-session.json")
        window = FakeWindow()
        app._window = window

        app._register_native_drop()

        self.assertEqual(window.dom.body.listeners[0][0], "drop")
        self.assertIn("window.previewmdNativeDropReady = true", window.scripts)


class SaveAsTests(unittest.TestCase):
    def make_app(self, temporary_directory):
        app = PreviewApp(session_path=str(Path(temporary_directory) / "session.json"))
        app._window = FakeWindow()
        return app

    def test_save_as_writes_extension_and_starts_watching(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "chapter"
            app = self.make_app(temporary_directory)
            app._window = FakeWindow(str(target))
            started = []
            with mock.patch.object(app, "_start_watching", side_effect=lambda path, content: started.append(path)):
                response = app._api.save_file_as("Untitled.md", "hello", None)

            created = Path(temporary_directory) / "chapter.md"
            self.assertTrue(response["ok"])
            self.assertEqual(response["path"], str(created.resolve()))
            self.assertEqual(response["name"], "chapter.md")
            self.assertEqual(response["hash"], content_hash("hello"))
            self.assertEqual(created.read_text(encoding="utf-8"), "hello")
            self.assertEqual(started, [str(created.resolve())])

    def test_save_as_updates_an_existing_watcher_hash(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "note.md"
            target.write_text("old", encoding="utf-8")
            app = self.make_app(temporary_directory)
            normalized = app._normalize_path(target)
            app._watchers[normalized] = {"observer": None, "hash": content_hash("old"), "token": 1}
            app._window = FakeWindow(str(target))

            response = app._api.save_file_as("note.md", "new", str(target))

            self.assertTrue(response["ok"])
            self.assertEqual(app._watchers[normalized]["hash"], content_hash("new"))
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_save_as_rejects_a_path_open_in_another_tab(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "open.md"
            target.write_text("open", encoding="utf-8")
            app = self.make_app(temporary_directory)
            app._watchers[app._normalize_path(target)] = {"observer": None, "hash": None, "token": 1}
            app._window = FakeWindow(str(target))

            response = app._api.save_file_as("Untitled.md", "draft", None)

            self.assertFalse(response["ok"])
            self.assertIn("already open", response["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), "open")

    def test_save_as_cancellation_matches_export_dialog_handling(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = self.make_app(temporary_directory)
            for dialog_result in (None, [], ""):
                app._window = FakeWindow(dialog_result)
                self.assertTrue(app._api.save_file_as("Untitled.md", "x", None)["cancelled"])

    def test_save_document_rejects_unsupported_destinations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = self.make_app(temporary_directory)
            with self.assertRaises(ValueError):
                app.save_document(Path(temporary_directory) / "note.pdf", "x")
            with self.assertRaises(ValueError):
                app.save_document(Path(temporary_directory) / "gone" / "note.md", "x")

    def test_save_as_uses_the_last_directory_and_reports_display_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = str(Path(temporary_directory).resolve())
            target = Path(temporary_directory) / "note.md"
            app = self.make_app(temporary_directory)
            app._last_directory = directory
            app._window = FakeWindow(str(target))

            with mock.patch.object(app, "_start_watching"):
                response = app._api.save_file_as("Untitled.md", "x", None)

            _, kwargs = app._window.dialogs[-1]
            self.assertEqual(kwargs["directory"], directory)
            self.assertTrue(response["display_path"])
            self.assertEqual(response["display_path"], response["path"])

    def test_save_document_and_load_file_remember_the_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = str(Path(temporary_directory).resolve())
            target = Path(temporary_directory) / "note.md"
            target.write_text("body", encoding="utf-8")
            app = self.make_app(temporary_directory)

            with mock.patch.object(app, "_start_watching"):
                app.save_document(target, "body")
                self.assertEqual(app._last_directory, directory)

            app._last_directory = ""
            app._window = FakeWindow()
            with mock.patch.object(app, "_start_watching"):
                app.load_file(target)
            self.assertEqual(app._last_directory, directory)

    def test_display_path_abbreviates_the_home_directory(self):
        with mock.patch("preview.os.path.expanduser", return_value="/Users/tester"):
            self.assertEqual(display_path("/Users/tester/Notes/a.md"), "~/Notes/a.md")
            self.assertEqual(display_path("/Users/tester"), "~")
            self.assertEqual(display_path("/tmp/a.md"), "/tmp/a.md")
            self.assertEqual(display_path(""), "")

    def test_helpers_keep_markdown_extension_and_sanitize_names(self):
        self.assertEqual(safe_save_filename(""), "Untitled.md")
        self.assertEqual(safe_save_filename("draft.txt"), "draft.txt")
        self.assertEqual(safe_save_filename("a/b:c?.md"), "b_c_.md")
        self.assertEqual(ensure_document_extension("/tmp/note"), "/tmp/note.md")
        self.assertEqual(ensure_document_extension("/tmp/note.markdown"), "/tmp/note.markdown.md")
        self.assertEqual(ensure_document_extension("/tmp/note.txt"), "/tmp/note.txt")


class SharedObserverTests(unittest.TestCase):
    def test_files_share_one_observer_and_directory_watch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = (root / "a.md").resolve()
            second = (root / "b.md").resolve()
            third = (root / "sub").resolve()
            third.mkdir()
            third = (third / "c.md").resolve()
            for path in (first, second, third):
                path.write_text("x", encoding="utf-8")

            app = PreviewApp(session_path=str(root / "session.json"))
            observer = mock.Mock()
            watch = mock.Mock()
            observer.schedule.return_value = watch

            with mock.patch("preview.Observer", return_value=observer) as observer_class:
                app._start_watching(str(first), "a")
                app._start_watching(str(second), "b")
                app._start_watching(str(third), "c")

                self.assertEqual(observer_class.call_count, 1)
                self.assertEqual(observer.start.call_count, 1)
                self.assertEqual(observer.schedule.call_count, 2)
                self.assertEqual(observer.add_handler_for_watch.call_count, 1)

                app.unwatch_file(str(first))
                observer.remove_handler_for_watch.assert_called_once()
                observer.unschedule.assert_not_called()
                self.assertIn(str(second), app._watchers)

                app.unwatch_file(str(second))
                observer.unschedule.assert_called_once_with(watch)

            app._cleanup()
            observer.stop.assert_called_once()


class FeatureMarkerTests(unittest.TestCase):
    def test_new_workspace_features_are_exposed_to_the_page(self):
        from preview import build_html

        document = build_html()
        for marker in (
            "function sessionState()",
            "function syncSession()",
            "window.finishSessionRestore = function(state)",
            "function newUntitledTab()",
            "function saveFileAs()",
            "window.previewmdNativeDropReady = false;",
            'id="tab-new"',
            "save_file_as",
            "function saveFile()",
            "if (!tab.path) return saveFileAs();",
            "function ensureDocumentPath()",
            "ensureDocumentPath().then(function(ready)",
            "Image was not inserted because the document has no location yet.",
            "response.display_path || response.name",
            "tabState.path ? (tabState.name + stateLabel + '\\n' + tabState.path)",
            "function refreshTab(tabState)",
            "function tabElement(tabState)",
            "var lastSentTitle = null;",
            "function reloadActiveTab()",
            "async function reloadActiveTab()",
            "reload_file",
            'id="tab-reload"',
            'id="conflict-banner"',
            "function applyDiskSnapshot(tab, content, diskHash)",
            "function keepLocalEdits()",
            "conflictBannerDismissed",
            "if (tab.dirty && !window.confirm('This tab has unsaved edits. Discard them and load the version on disk?')) return;",
            "function undoEditor()",
            "function redoEditor()",
            "function commitUndoSnapshot(tab)",
            "editorFocused && meta && key === 'z'",
            "tab.imageCache = {};",
            "IMAGE_CACHE_MAX_ENTRIES",
        ):
            self.assertIn(marker, document)


if __name__ == "__main__":
    unittest.main()
