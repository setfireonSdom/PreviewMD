import tempfile
import unittest
from pathlib import Path

from preview import build_html, build_standalone_html, inline_css_resources, safe_export_filename


class ExportHelperTests(unittest.TestCase):
    def test_export_filename_is_a_safe_suggestion(self):
        self.assertEqual(safe_export_filename("notes.md"), "notes.html")
        self.assertEqual(safe_export_filename("../bad:name?.md"), "bad_name_.html")
        self.assertEqual(safe_export_filename(""), "document.html")

    def test_standalone_html_escapes_title_and_contains_only_supplied_body(self):
        document = build_standalone_html(
            'A < B & "quoted"',
            '<h1 id="previewmd-heading-a">A</h1>',
            ".markdown-body { color: black; }",
        )

        self.assertIn('<meta charset="utf-8">', document)
        self.assertIn("<title>A &lt; B &amp; &quot;quoted&quot;</title>", document)
        self.assertIn('<main class="markdown-body"><h1', document)
        self.assertNotIn("id=\"tab-bar\"", document)
        self.assertNotIn("<script", document)

    def test_standalone_stylesheet_cannot_close_style_element(self):
        document = build_standalone_html("Title", "<p>Body</p>", "x</style><script>bad()</script>")
        self.assertNotIn("</style><script>bad()", document)


class BuildHtmlSmokeTests(unittest.TestCase):
    def test_release_markers_are_present(self):
        document = build_html()
        for marker in (
            'id="toc-sidebar"',
            'id="lightbox"',
            "function syncProportionalScroll",
            "function exportHtml",
            "Conflict: this file changed on disk",
            "ASCII_CONTROL_NORMALIZATION",
            "beforeunload",
            "awaitStableExportRender",
            "@media print",
        ):
            self.assertIn(marker, document)

        self.assertIn("data:font/woff2;base64,", document)
        self.assertNotIn("url(fonts/KaTeX", document)


class CssResourceTests(unittest.TestCase):
    def test_safe_font_is_inlined_and_missing_fallback_is_neutralized(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fonts = root / "fonts"
            fonts.mkdir()
            (fonts / "Test.woff2").write_bytes(b"font bytes")
            css = 'src:url(fonts/Test.woff2) format("woff2"),url(fonts/Test.woff) format("woff")'

            inlined = inline_css_resources(css, root)

            self.assertIn("data:font/woff2;base64,", inlined)
            self.assertIn("data:application/octet-stream;base64,", inlined)
            self.assertNotIn("fonts/Test", inlined)

    def test_missing_required_or_unsafe_font_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "fonts").mkdir()
            with self.assertRaises(FileNotFoundError):
                inline_css_resources("src:url(fonts/Missing.woff2)", root)
            with self.assertRaises(ValueError):
                inline_css_resources("src:url(../secret.woff2)", root)
            with self.assertRaises(ValueError):
                inline_css_resources("src:url(https://example.com/font.woff2)", root)


if __name__ == "__main__":
    unittest.main()
