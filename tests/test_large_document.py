import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from preview import build_html


class ChunkedParsingTests(unittest.TestCase):
    """A single marked lexer pass is superlinear in JavaScriptCore; the app must
    parse long documents in blank-line separated chunks instead."""

    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_render_pipeline_parses_in_chunks(self):
        for marker in (
            "var MARKDOWN_CHUNK_MAX_CHARS = 1000;",
            "function splitMarkdownChunks(text)",
            "template.innerHTML = marked.parse(prefix + chunks[mounted]);",
            "sanitizeFragment(template.content);",
            "function markdownChunkBoundaryIsInsideList(lines, index)",
            "var LINK_DEFINITION_RE = ",
        ):
            self.assertIn(marker, self.document)

    def test_render_mounts_in_progressive_batches(self):
        for marker in (
            "var PROGRESSIVE_RENDER_BUDGET_MS = 14;",
            "var PROGRESSIVE_MIN_CHUNKS = 30;",
            "function mountBatch(budgetStart, minChunks)",
            "function createRenderScheduler()",
            "new MessageChannel()",
            "scheduleStep(step);",
        ):
            self.assertIn(marker, self.document)


class LazyMathTests(unittest.TestCase):
    """KaTeX costs ~0.4 ms per formula; formula-heavy notes must not block first
    paint, and export/print must still contain every formula."""

    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_math_is_deferred_behind_an_observer(self):
        for marker in (
            "var MATH_EAGER_NODE_LIMIT = 150;",
            "function prepareMathCandidates(root)",
            "function scheduleMathRendering(candidates, contentElement)",
            "new IntersectionObserver(function(entries)",
            "function forceRenderAllMath()",
        ):
            self.assertIn(marker, self.document)

    def test_export_and_print_force_all_formulas(self):
        self.assertEqual(self.document.count("forceRenderAllMath();"), 2)

    def test_escaped_dollars_are_restored_per_node(self):
        for marker in (
            "function restoreEscapedDollarsInElement(root)",
            "node.nodeValue = restoreEscapedDollars(node.nodeValue);",
        ):
            self.assertIn(marker, self.document)


class HighlightingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_unlabelled_code_uses_a_bounded_language_subset(self):
        self.assertIn("var HIGHLIGHT_AUTO_LANGUAGES = [", self.document)
        self.assertIn("hljs.highlightAuto(token.text, HIGHLIGHT_AUTO_LANGUAGES)", self.document)
        self.assertNotIn("hljs.highlightAuto(token.text).value", self.document)

    def test_chunker_never_splits_fences_math_or_lists(self):
        for marker in (
            "var fenceOpen = line.match(",
            "var dollarCount = (line.match(",
            "if (markdownChunkBoundaryIsInsideList(lines, i)) {",
        ):
            self.assertIn(marker, self.document)

    def test_single_lexer_pass_is_no_longer_used(self):
        self.assertNotIn("sanitizeHtml(marked.parse(", self.document)


@unittest.skipIf(shutil.which("node") is None, "node is required to run the chunker")
class DisplayMathBlockTests(unittest.TestCase):
    """A display formula written across several lines must not be handed to the
    renderer as several lines.

    PreviewMD enables the GFM breaks behaviour, so a newline becomes a <br>
    element. The display-math pattern then sees `$$`, <br>, the formula, <br>,
    `$$` and matches nothing, and the page shows raw LaTeX. The chunker already
    tracks where a $$ block starts and ends, so it joins those lines; LaTeX reads
    a newline as a space, so the formula itself is unchanged.
    """

    CHUNKER_FUNCTIONS = (
        "markdownLineLooksLikeListItem",
        "markdownLineIsIndented",
        "markdownLineContinuesList",
        "markdownLineWithoutQuotePrefix",
        "markdownChunkBoundaryIsInsideList",
        "collapseDisplayMathLines",
        "splitMarkdownChunks",
    )

    DRIVER = """
    var CASES = __CASES__;
    var out = {};
    CASES.forEach(function (entry) {
        out[entry[0]] = splitMarkdownChunks(entry[1]);
    });
    console.log(JSON.stringify(out));
    """

    @classmethod
    def setUpClass(cls):
        document = build_html()
        parts = []
        for name in cls.CHUNKER_FUNCTIONS:
            match = re.search(rf"function {name}\(.*?\n\}}", document, re.S)
            if match is None:
                raise AssertionError(f"missing function {name}")
            parts.append(match.group(0))
        variables = re.search(r"var MARKDOWN_CHUNK_MAX_CHARS = \d+;", document).group(0)
        variables += "\n" + re.search(r"var LINK_DEFINITION_RE = .*?;", document).group(0)
        cases = (
            ("multi line", "前\n\n$$\na^2+b^2\n$$\n\n后"),
            ("one line", "前\n\n$$a^2+b^2$$\n\n后"),
            ("indented body", "前\n\n$$\n  \\frac{a}{b}\n$$\n\n后"),
            ("blank line inside", "前\n\n$$\na^2\n\nb^2\n$$\n\n后"),
            ("in a fence", "前\n\n```latex\n$$\na^2\n$$\n```\n\n后"),
            ("in a tilde fence", "前\n\n~~~latex\n$$\na^2\n$$\n~~~\n\n后"),
            ("unterminated", "前\n\n$$\na^2\n\n后"),
            ("escaped dollars", "价格 \\$5 与 \\$10"),
            ("inline math", "前 $a^2$ 后"),
        )
        driver = (
            variables
            + "\n"
            + "\n".join(parts)
            + "\n"
            + cls.DRIVER.replace("__CASES__", json.dumps(cases, ensure_ascii=False))
        )
        result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        cls.chunks = {
            name: payload["chunks"] for name, payload in json.loads(result.stdout.strip()).items()
        }

    def joined(self, name):
        return "\n".join(self.chunks[name])

    def test_a_multi_line_formula_becomes_one_line(self):
        # One line means one text node, which means the display-math pattern can
        # see the whole formula.
        self.assertIn("$$ a^2+b^2 $$", self.joined("multi line"))
        self.assertIn("$$   \\frac{a}{b} $$", self.joined("indented body"))
        # The blank line becomes the space it already was to LaTeX, which reads a
        # newline as whitespace.
        self.assertIn("$$ a^2  b^2 $$", self.joined("blank line inside"))

    def test_a_one_line_formula_is_untouched(self):
        self.assertIn("$$a^2+b^2$$", self.joined("one line"))
        self.assertIn("$a^2$", self.joined("inline math"))

    def test_a_fenced_formula_is_never_touched(self):
        # A notes page that shows LaTeX in a code fence must keep its source.
        for name in ("in a fence", "in a tilde fence"):
            with self.subTest(case=name):
                joined = self.joined(name)
                self.assertIn("$$\na^2\n$$", joined)

    def test_text_that_is_not_a_formula_is_left_alone(self):
        # An unterminated $$ was never a formula: its lines stay as they are,
        # rather than pulling the rest of the document onto one line.
        self.assertIn("$$\na^2", self.joined("unterminated"))
        self.assertIn("\\$5 与 \\$10", self.joined("escaped dollars"))

    def test_a_formula_is_never_split_across_chunks(self):
        # A split formula would leave one chunk holding an odd number of $$; the
        # unterminated opener is the only case where an odd count is legitimate.
        for name, chunks in self.chunks.items():
            if name == "unterminated":
                continue
            for chunk in chunks:
                with self.subTest(case=name):
                    self.assertEqual(
                        chunk.count("$$") % 2,
                        0,
                        f"{name}: a formula was cut in half by the chunker",
                    )


class PreviewSearchTests(unittest.TestCase):
    """Preview search must not re-render the document or build one DOM node per
    hit; matches are ranges painted by the engine."""

    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_preview_search_uses_ranges_and_custom_highlight(self):
        for marker in (
            "function collectPreviewRanges(root, query)",
            "function paintPreviewHighlights()",
            "function scrollToRange(index)",
            "supportsCustomHighlight()",
            "CSS.highlights.set('previewmd-search'",
            "::highlight(previewmd-search) {",
        ):
            self.assertIn(marker, self.document)

    def test_preview_search_is_bounded_and_debounced(self):
        for marker in (
            "var SEARCH_PAINT_LIMIT = 800;",
            "var SEARCH_RANGE_LIMIT = 20000;",
            "var SEARCH_DEBOUNCE_MS = 120;",
            "searchInputTimer = setTimeout(doSearch, SEARCH_DEBOUNCE_MS);",
        ):
            self.assertIn(marker, self.document)

    def test_dom_marking_helper_is_gone(self):
        self.assertNotIn("function highlightQueryInElement(", self.document)

    def test_preview_search_keeps_the_editor_branch(self):
        for marker in (
            "searchMatches = collectEditorMatches(editor.value, query);",
            "if (searchMatches.length > 0 && !searchComposing) {",
            "selectEditorMatch(searchMatches[0].start, searchMatches[0].end);",
        ):
            self.assertIn(marker, self.document)


class ScrollResponsivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_active_heading_updates_are_throttled_and_skipped_when_hidden(self):
        for marker in (
            "function scheduleActiveHeadingUpdate()",
            "activeHeadingFrame = requestAnimationFrame(function() {",
            "if (!document.body.classList.contains('toc-open')) return;",
        ):
            self.assertIn(marker, self.document)
        # Opening the sidebar rebuilds the outline first, then marks the entry
        # that is currently on screen.
        self.assertIn("if (open) rebuildToc(true);\n    updateActiveHeading();\n}", self.document)

    def test_active_heading_touches_only_the_changed_button(self):
        for marker in (
            "var tocButtons = new Map();",
            "var previous = tocButtons.get(activeTocId);",
            "var current = tocButtons.get(activeTocId);",
        ):
            self.assertIn(marker, self.document)
        self.assertNotIn("document.querySelectorAll('#toc-list button').forEach", self.document)


class LiveRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_split_preview_backs_off_on_large_documents(self):
        for marker in (
            "var LIVE_RENDER_BASE_MS = 200;",
            "var LIVE_RENDER_MAX_MS = 1200;",
            "function liveRenderDelay(text)",
            "liveRenderDelay(tab.content)",
        ):
            self.assertIn(marker, self.document)


class EditorHighlightTests(unittest.TestCase):
    """Editor search must not build one <mark> per match over the whole document."""

    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_editor_mirror_is_text_only_and_engine_painted(self):
        for marker in (
            "function clearEditorHighlights()",
            "function paintEditorHighlights()",
            "function editorSearchPaintWindow()",
            "CSS.highlights.set('previewmd-editor-search'",
            "layer.textContent = text;",
            "var editorLayerText = null;",
            "::highlight(previewmd-editor-search) {",
        ):
            self.assertIn(marker, self.document)

    def test_editor_mirror_no_longer_builds_a_mark_per_match(self):
        self.assertNotIn("mark.textContent = text.slice(match.start, match.end);", self.document)


@unittest.skipUnless(shutil.which("node"), "node is required to parse the bundled JavaScript")
class GeneratedJavaScriptSyntaxTests(unittest.TestCase):
    """Guard against f-string escaping mistakes (for example `\\n` turning into a
    real newline) that would only show up inside the running webview."""

    def test_every_script_block_parses(self):
        blocks = re.findall(r"<script[^>]*>(.*?)</script>", build_html(), re.S)
        self.assertGreaterEqual(len(blocks), 4)
        with tempfile.TemporaryDirectory() as directory:
            for index, block in enumerate(blocks):
                path = Path(directory) / f"block{index}.js"
                path.write_text(block, encoding="utf-8")
                result = subprocess.run(
                    ["node", "--check", str(path)], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, f"script block {index}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
