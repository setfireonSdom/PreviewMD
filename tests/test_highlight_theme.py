import unittest

from preview import build_html


class HighlightThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_theme_has_light_dark_and_print_palettes(self):
        self.assertIn("--code-bg: #f0eee7", self.document)
        self.assertIn("--code-bg: #2a2823", self.document)
        self.assertIn("--code-bg: #ffffff", self.document)
        self.assertIn("@media (prefers-color-scheme: dark)", self.document)
        self.assertIn("@media print", self.document)

    def test_code_block_owns_spacing_and_background(self):
        self.assertIn("background: var(--code-bg)", self.document)
        self.assertIn("border: 1px solid var(--code-border)", self.document)
        self.assertIn("pre code.hljs {\n    display: block;", self.document)
        self.assertIn("overflow-x: visible;\n    padding: 0;", self.document)

    def test_legacy_dark_only_theme_is_not_embedded(self):
        self.assertNotIn("Theme: GitHub Dark", self.document)
        self.assertNotIn("background:#0d1117", self.document)


if __name__ == "__main__":
    unittest.main()
