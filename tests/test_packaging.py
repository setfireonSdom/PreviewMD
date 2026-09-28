import re
import unittest
from pathlib import Path

import app_metadata

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _RecordingEvent:
    """Stands in for a pywebview event: `handlers += fn` is how you subscribe."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _RecordingWindow:
    def __init__(self):
        self.events = type("Events", (), {"loaded": _RecordingEvent(), "closing": _RecordingEvent()})()


class WindowEventSubscriptionTests(unittest.TestCase):
    """`loaded` fires while `webview.start()` is running, so a handler attached
    after it returns never fires. That is not a theoretical hazard: it left the
    app opening an empty window, ignoring both the file given on the command
    line and the session it was supposed to restore."""

    def setUp(self):
        import preview

        self.preview = preview
        self.created = []
        self.at_start = []

        app = preview.PreviewApp.__new__(preview.PreviewApp)
        app._pending_files = []
        app._session = {"tabs": [], "active": 0, "view_mode": "preview",
                        "positions": {}, "drafts": [], "active_draft": None}
        app._settings = {}
        app._api = None
        app._window = None
        app._on_loaded = lambda: None
        app._on_closing = lambda: True
        app._cleanup = lambda: None
        self.app = app

    def _run(self, first_start_fails=False):
        """Drive PreviewApp.run() against a webview that records the order."""

        def create_window(*args, **kwargs):
            self.created.append(_RecordingWindow())
            return self.created[-1]

        def start(debug=False, **kwargs):
            window = self.created[-1]
            self.at_start.append((len(window.events.loaded.handlers),
                                  len(window.events.closing.handlers)))
            if first_start_fails and len(self.at_start) == 1:
                raise RuntimeError("menu unavailable")

        original_webview = self.preview.webview
        original_menu = self.preview.build_application_menu
        self.preview.webview = type(
            "webview",
            (),
            {"create_window": staticmethod(create_window), "start": staticmethod(start)},
        )()
        self.preview.build_application_menu = lambda _app: None
        try:
            self.app.run()
        finally:
            self.preview.webview = original_webview
            self.preview.build_application_menu = original_menu
        return self.at_start

    def test_handlers_are_attached_before_the_webview_loop_starts(self):
        at_start = self._run()
        self.assertEqual(len(at_start), 1)
        self.assertEqual(at_start[0], (1, 1))

    def test_the_retry_window_is_subscribed_too(self):
        # pywebview builds the native menu inside start(), so that is where a
        # bad menu surfaces. The retry creates a second window; handlers left
        # behind on the first would leave that window just as inert.
        at_start = self._run(first_start_fails=True)
        self.assertEqual(len(self.created), 2, "the menu retry should build a second window")
        self.assertEqual(len(at_start), 2)
        self.assertEqual(at_start[-1], (1, 1))


class MetadataTests(unittest.TestCase):
    def test_metadata_is_a_single_conventional_source(self):
        self.assertEqual(app_metadata.APP_NAME, "PreviewMD")
        self.assertRegex(app_metadata.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertRegex(app_metadata.BUNDLE_IDENTIFIER, r"^[a-z0-9.]+$")


class SpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = (PROJECT_ROOT / "PreviewMD.spec").read_text(encoding="utf-8")

    def test_spec_is_portable_and_reads_shared_metadata(self):
        self.assertIn("SPECPATH", self.spec)
        self.assertIn("app_metadata.py", self.spec)
        self.assertNotIn("/Users/", self.spec)
        for key in ("APP_NAME", "APP_VERSION", "BUNDLE_IDENTIFIER"):
            self.assertIn(f"metadata['{key}']", self.spec)

    def test_spec_declares_markdown_and_text_document_types(self):
        self.assertIn("CFBundleDocumentTypes", self.spec)
        for document_type in ("'md'", "'txt'", "net.daringfireball.markdown", "public.plain-text"):
            self.assertIn(document_type, self.spec)
        self.assertIn("UTImportedTypeDeclarations", self.spec)


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (PROJECT_ROOT / "build.sh").read_text(encoding="utf-8")

    def test_build_script_uses_project_python_and_checked_in_spec(self):
        self.assertIn("PYTHON_BIN", self.script)
        self.assertIn(".venv/bin/python", self.script)
        self.assertIn("PreviewMD.spec", self.script)
        self.assertIn("app_metadata.py", self.script)
        self.assertNotIn("pip install", self.script)

    def test_build_script_signs_and_packages_a_dmg(self):
        for tool in ("codesign", "hdiutil", "ditto"):
            self.assertIn(tool, self.script)
        self.assertIn("--noconfirm", self.script)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = PROJECT_ROOT / ".github" / "workflows" / "build-release.yml"
        cls.workflow = workflow.read_text(encoding="utf-8")

    def test_workflow_tests_builds_and_uploads_without_publishing(self):
        self.assertIn("macos-", self.workflow)
        self.assertIn("unittest discover", self.workflow)
        self.assertIn("bash build.sh", self.workflow)
        self.assertIn("upload-artifact", self.workflow)
        self.assertIn("dist/*/*.dmg", self.workflow)

    def test_publishing_requires_an_explicit_dispatch_input(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertRegex(self.workflow, re.compile(r"publish:.*\n(?:.*\n)*?.*default: false"))
        self.assertIn("inputs.publish == true", self.workflow)

    def test_workflow_builds_both_apple_silicon_and_intel(self):
        # Assert on the runs-on lines, not on the file: the comments next to
        # them still name the retired images, and matching those would let the
        # real labels drift to anything while the test stayed green.
        labels = [label for label in re.findall(r"runs-on:\s*(\S+)", self.workflow) if label.startswith("macos")]
        self.assertEqual(labels, ["macos-15", "macos-15-intel"])
        self.assertIn("build-intel", self.workflow)
        self.assertIn("needs: [build, build-intel]", self.workflow)


class ApplicationMenuTests(unittest.TestCase):
    """A Mac app needs a native File menu; it must never be able to stop startup."""

    def setUp(self):
        from webview import Menu
        from webview.menu import MenuAction, MenuSeparator

        from preview import build_application_menu

        self.Menu = Menu
        self.MenuAction = MenuAction
        self.MenuSeparator = MenuSeparator
        self.build_application_menu = build_application_menu

        class FakeWindow:
            def __init__(self):
                self.built = []

            def evaluate_js(self, script):
                self.built.append(script)

        class FakeApp:
            _window = FakeWindow()

            class _api:
                @staticmethod
                def open_file():
                    return None

        self.app = FakeApp()
        self.window = FakeApp._window
        self.menu = self.build_application_menu(self.app)

    def labels(self, section):
        return [getattr(item, "title", None) for item in section.items]

    def test_menu_uses_the_types_pywebview_expects(self):
        # A plain dict here crashes inside webview.start() with
        # "'dict' object has no attribute 'title'".
        for section in self.menu:
            self.assertIsInstance(section, self.Menu)
            for item in section.items:
                self.assertTrue(
                    isinstance(item, (self.MenuAction, self.MenuSeparator, self.Menu)),
                    type(item),
                )

    def test_menu_adds_only_a_file_menu(self):
        # pywebview contributes the standard View and Edit menus itself, and a
        # second menu with the same title would appear twice in the menu bar.
        self.assertEqual([section.title for section in self.menu], ["File"])

    def test_file_menu_has_open_new_save_reload_close_and_reopen(self):
        self.assertEqual(
            self.labels(self.menu[0]),
            [
                "Open…",
                "New Document",
                "Save",
                "Save As…",
                "Reload from Disk",
                None,  # separator
                "Close Tab",
                "Reopen Closed Tab",
            ],
        )
        self.assertIsInstance(self.menu[0].items[5], self.MenuSeparator)

    def test_menu_actions_dispatch_into_the_page(self):
        close_tab = self.menu[0].items[6].function
        reopen = self.menu[0].items[7].function
        new_document = self.menu[0].items[1].function

        close_tab()
        reopen()
        new_document()

        self.assertEqual(
            self.window.built,
            [
                "window.closeActiveTab()",
                "window.reopenClosedTab()",
                "window.newUntitledTab()",
            ],
        )

    def test_menu_does_not_duplicate_pywebview_default_menus(self):
        titles = [section.title for section in self.menu]
        self.assertNotIn("View", titles)
        self.assertNotIn("Edit", titles)
        self.assertNotIn("__app__", titles)


class OpenSourceBaselineTests(unittest.TestCase):
    """A public repository needs a licence, a changelog, and install guidance."""

    @classmethod
    def setUpClass(cls):
        cls.readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    def test_repository_ships_an_mit_licence(self):
        licence = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", licence)
        self.assertIn("Copyright (c) 2026 setfireonSdom", licence)
        self.assertIn("WITHOUT WARRANTY OF ANY KIND", licence)

    def test_repository_ships_a_changelog(self):
        changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("# 更新日志", changelog)
        self.assertIn("0.1.0", changelog)

    def test_readme_explains_installing_from_source_and_from_a_dmg(self):
        for marker in (
            "## 快速开始",
            "git clone https://github.com/setfireonSdom/PreviewMD.git",
            "xattr -dr com.apple.quarantine",
        ):
            self.assertIn(marker, self.readme)

    def test_readme_is_honest_about_signing_and_remote_images(self):
        # The app is ad-hoc signed only, so the Gatekeeper workaround must be documented.
        self.assertIn("ad-hoc 签名", self.readme)
        self.assertIn("没有**付费的 Apple 开发者签名与公证", self.readme)
        # Remote images really do make network requests, so the privacy cost is stated.
        self.assertIn("### 远程图片与隐私", self.readme)


if __name__ == "__main__":
    unittest.main()
