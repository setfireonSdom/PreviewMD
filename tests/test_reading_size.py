"""The reading size control, and the ordering trap that nearly shipped it.

The first version applied the saved size from the middle of the script, while
`var FONT_SIZE_STEPS` was declared near the end. `var` hoists the declaration
but not the assignment, so the call saw an undefined list, `.forEach` threw, and
because it ran inside the main script block every statement after it was skipped:
window.addTabFromPython was never defined and the whole editor was dead.

`node --check` cannot see that, and neither can a substring assertion. What
catches it is a check on the order of two statements in the generated page, which
is exactly what the property is about.
"""

import tempfile
import unittest
from pathlib import Path

import preview
from preview import (
    build_html,
    load_settings,
    normalize_settings,
    settings_file_path,
    store_settings,
)


class SettingsStoreTests(unittest.TestCase):
    def test_defaults_survive_a_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "nope.json"
            self.assertEqual(load_settings(missing)["font_size"], "default")
            broken = Path(directory) / "broken.json"
            broken.write_text("{ not json", encoding="utf-8")
            self.assertEqual(load_settings(broken)["font_size"], "default")

    def test_a_known_size_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertTrue(store_settings({"font_size": "huge"}, path))
            self.assertEqual(load_settings(path)["font_size"], "huge")

    def test_unknown_keys_and_values_are_dropped(self):
        settings = normalize_settings({"font_size": "enormous", "theme": "dark", "x": 1})
        self.assertEqual(settings, {"version": preview.SETTINGS_VERSION, "font_size": "default"})

    def test_settings_live_outside_the_session_file(self):
        # The session is only restored when no files are given, so a preference
        # stored there would be ignored on every launch from Finder.
        self.assertNotEqual(settings_file_path(), preview.session_file_path())
        with tempfile.TemporaryDirectory() as directory:
            environment = {"PREVIEWMD_SETTINGS_FILE": f"{directory}/s.json"}
            self.assertTrue(settings_file_path(environment).endswith("s.json"))


class ReadingSizePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_initialisation_runs_after_the_data_it_needs_exists(self):
        # The regression this file is about: calling this before FONT_SIZE_STEPS
        # is assigned throws, and the throw kills the rest of the script block.
        declaration = self.document.index("var FONT_SIZE_STEPS = [")
        call = self.document.index("initReadingSize(window.previewmdSettings);")
        self.assertLess(
            declaration,
            call,
            "initReadingSize must run after FONT_SIZE_STEPS is assigned, "
            "not merely declared",
        )

    def test_both_panes_follow_one_setting(self):
        # The editor is a textarea with its own size, and the search highlight
        # mirror copies computed styles once per text change; all three have to
        # move together or the panes disagree.
        self.assertIn("font-size: var(--reader-font-size, 16px);", self.document)
        self.assertIn("font-size: var(--editor-font-size, 15px);", self.document)
        self.assertIn("root.style.setProperty('--reader-font-size', step.content + 'px');", self.document)
        self.assertIn("root.style.setProperty('--editor-font-size', step.editor + 'px');", self.document)
        self.assertIn("editorLayerText = null;", self.document)

    def test_the_menu_is_reachable_and_reports_the_current_choice(self):
        self.assertIn('id="settings-menu"', self.document)
        self.assertIn('role="menuitemradio"', self.document)
        self.assertIn("'aria-checked'", self.document)
        self.assertIn("button.setAttribute('aria-checked'", self.document)

    def test_changing_the_size_is_persisted(self):
        self.assertIn("callBridge('save_settings', { font_size: key })", self.document)

    def test_python_persists_and_reloads_the_preference(self):
        import inspect

        source = inspect.getsource(preview)
        self.assertIn("def save_settings(self, settings):", source)
        self.assertIn("self._settings = load_settings()", source)

    def test_a_failed_write_does_not_break_the_session(self):
        # A preference that could not be stored is still applied for this run.
        import inspect

        source = inspect.getsource(preview)
        self.assertIn("return {\"ok\": True, \"saved\": stored}", source)


if __name__ == "__main__":
    unittest.main()
