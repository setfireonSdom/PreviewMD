"""Word count behaviour, driven through the real functions from the built page.

A string assertion can only prove that `countDocumentText` exists. It cannot
prove that a full-width comma is not counted as a Chinese character, which is
exactly the kind of mistake that makes a novel's word count quietly wrong. These
tests extract the shipped functions and run them, so a regression has to be
deliberate.
"""

import json
import re
import shutil
import subprocess
import unittest

from preview import build_html, build_standalone_html

FUNCTIONS = (
    "isWordIdeograph",
    "isWordLetter",
    "isWordSpace",
    "countDocumentText",
    "formatCount",
)

# Each case is (name, document text, expected words, expected characters).
# Characters never count whitespace; words count nothing for punctuation, so
# every case where the two numbers differ is a case about what a character is.
CASES = (
    ("empty", "", 0, 0),
    ("whitespace only", "  \n\t\n", 0, 0),
    ("one chinese character", "字", 1, 1),
    # 4 ideographs and 2 full-width marks: the marks are characters, never words.
    ("chinese sentence", "你好，世界！", 4, 6),
    ("chapter heading", "第一章 天亮了", 6, 6),
    ("english words", "Hello world", 2, 10),
    ("english sentence", "It was a dark night.", 5, 16),
    ("contraction", "don't stop", 2, 9),
    ("curly contraction", "isn’t it", 2, 7),
    ("numbers", "Room 101 2024", 3, 11),
    ("hyphen stays split", "well-known", 2, 10),
    ("fullwidth latin", "ＮＩＫＥＹ", 1, 5),
    ("kana", "ひらがなカタカナ", 8, 8),
    ("hangul", "한국어", 3, 3),
    # An emoji is a character and not a word; CJK extensions are both.
    ("emoji is a character, not a word", "hi \U0001f44b", 1, 3),
    ("cjk extension", "\U00020000\U00020001", 2, 2),
    # Markdown syntax is punctuation: it moves the character count, not the
    # word count, which is why counting the source is close enough.
    ("markdown heading", "# 标题\n\n正文。\n", 4, 6),
    ("markdown list", "- 第一项\n- 第二项\n", 6, 8),
    ("markdown emphasis", "**粗体**与*斜体*\n", 5, 11),
    ("markdown table", "| a | b |\n|---|---|\n| 1 | 2 |\n", 4, 19),
    ("code fence", "```py\nprint(1)\n```\n", 3, 16),
)

# A novel-sized document, one unit repeated until it is a couple of million
# characters. The time bound is a runaway guard, not a benchmark: anything that
# is not a single pass over the text (a regex or an array per character, a
# string rebuilt per character) misses it by orders of magnitude on any engine.
NOVEL_UNIT = "他抬头看了一眼窗外的雨。"
PROSE_UNIT = "The quick brown fox jumps over the lazy dog. "
LARGE_CASES = (
    ("novel", NOVEL_UNIT, 175_000, 11, 12),
    ("prose", PROSE_UNIT, 50_000, 9, 36),
)
RUNAWAY_BUDGET_SECONDS = 20

DRIVER = """
var CASES = __CASES__;
var out = {};
CASES.forEach(function (entry) {
    out[entry[0]] = countDocumentText(entry[1]);
});
var large = [];
__LARGE__.forEach(function (entry) {
    var text = entry[1].repeat(entry[2]);
    var started = process.hrtime.bigint();
    var counts = countDocumentText(text);
    large.push({
        name: entry[0],
        length: text.length,
        seconds: Number(process.hrtime.bigint() - started) / 1e9,
        counts: counts
    });
});
console.log(JSON.stringify({ small: out, large: large, formatted: formatCount(2165059) }));
"""


def extract_word_count_javascript() -> str:
    """Pull the counting helpers out of the generated page, verbatim."""
    document = build_html()
    parts = []
    for name in FUNCTIONS:
        match = re.search(rf"function {name}\(.*?\n\}}", document, re.S)
        if match is None:
            raise AssertionError(f"missing function {name}")
        parts.append(match.group(0))
    return "\n".join(parts)


def function_body(document: str, anchor: str) -> str:
    """The text of the function that starts at `anchor`, braces balanced."""
    start = document.index("{", document.index(anchor))
    depth = 0
    for index in range(start, len(document)):
        if document[index] == "{":
            depth += 1
        elif document[index] == "}":
            depth -= 1
            if depth == 0:
                return document[start : index + 1]
    raise AssertionError(f"unbalanced braces after {anchor}")


@unittest.skipIf(shutil.which("node") is None, "node is required to run the word count logic")
class WordCountBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = (
            extract_word_count_javascript()
            + "\n"
            + DRIVER.replace("__CASES__", json.dumps(CASES, ensure_ascii=False)).replace(
                "__LARGE__", json.dumps(LARGE_CASES, ensure_ascii=False)
            )
        )
        result = subprocess.run(["node", "-e", driver], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        payload = json.loads(result.stdout.strip())
        cls.counts = payload["small"]
        cls.large = payload["large"]
        cls.formatted = payload["formatted"]

    def test_every_kind_of_text_counts_the_way_wps_counts_it(self):
        for name, _text, words, characters in CASES:
            with self.subTest(case=name):
                counts = self.counts[name]
                self.assertEqual(counts["words"], words, f"{name}: wrong word count")
                self.assertEqual(counts["characters"], characters, f"{name}: wrong character count")

    def test_cjk_punctuation_is_a_character_but_never_a_word(self):
        # A naive "anything above U+2E80 is Chinese" test counts every full
        # width comma and full stop in the book, which is off by a wide margin.
        punctuation = self.counts["chinese sentence"]
        self.assertEqual(punctuation["words"], 4)
        self.assertGreater(punctuation["characters"], punctuation["words"])

    def test_markdown_syntax_moves_the_character_count_but_not_the_word_count(self):
        heading = self.counts["markdown heading"]
        self.assertEqual(heading["words"], 4)
        self.assertEqual(heading["characters"], 6)
        self.assertEqual(self.counts["markdown emphasis"]["words"], 5)

    def test_a_novel_sized_document_is_counted_in_one_pass(self):
        for name, _unit, repeats, words, characters in LARGE_CASES:
            with self.subTest(case=name):
                entry = next(item for item in self.large if item["name"] == name)
                self.assertGreater(entry["length"], 1_000_000)
                self.assertLess(
                    entry["seconds"],
                    RUNAWAY_BUDGET_SECONDS,
                    "counting a novel-sized document is not a single pass",
                )
                self.assertEqual(entry["counts"]["words"], words * repeats)
                self.assertEqual(entry["counts"]["characters"], characters * repeats)

    def test_counts_are_grouped_like_wps(self):
        self.assertEqual(self.formatted, "2,165,059")


class WordCountInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_the_status_bar_sits_below_the_workspace(self):
        self.assertIn('<div id="status-bar"', self.document)
        self.assertIn('<span id="status-words" class="stat">字数 0</span>', self.document)
        self.assertIn('<span id="status-characters" class="stat">字符 0</span>', self.document)
        self.assertLess(self.document.index('id="main-area"'), self.document.index('id="status-bar"'))
        self.assertLess(self.document.index('id="status-bar"'), self.document.index('id="lightbox"'))
        # Statistics belong to a document, so the empty window keeps no bar.
        self.assertIn("body.has-tabs #status-bar { display: flex; }", self.document)
        self.assertIn("#status-bar {\n    display: none;", self.document)

    def test_the_window_lays_out_as_a_column_so_the_bar_keeps_its_own_space(self):
        self.assertIn("body { display: flex; flex-direction: column; }", self.document)
        self.assertIn(
            "#main-area {\n    position: relative;\n    flex: 1 1 0px;\n    min-height: 0;",
            self.document,
        )
        # An unpinned tab bar is a flex item that shrinks by its share of the
        # line, and the workspace below is as tall as the document: on a long
        # page that squeezed the 36px tab bar down to a sliver.
        self.assertIn("#tab-bar {\n    display: none;\n    height: 36px;", self.document)
        self.assertIn("flex: 0 0 36px;", self.document)
        self.assertIn("#status-bar .stat { contain: layout style; }", self.document)

    def test_the_bar_updates_on_every_path_that_changes_the_text(self):
        for marker in (
            "updateDocumentStats(tab);",
            "scheduleDocumentStats(tab);",
            "function paintDocumentStats(tab)",
            "function scheduleDocumentStats(tab)",
            "setStatusStat('status-words', '字数 ' + formatCount(counts.words));",
            "setStatusStat('status-characters', '字符 ' + formatCount(counts.characters));",
        ):
            self.assertIn(marker, self.document)
        # Opening or switching a tab, undo/redo, an external reload and typing
        # are the four ways tab.content changes in the page.
        for anchor in (
            "function renderActiveTab()",
            "function applyEditorSnapshot(tab, content, selection)",
            "function applyDiskSnapshot(tab, content, diskHash)",
            "document.getElementById('editor').addEventListener('input'",
        ):
            body = function_body(self.document, anchor)
            self.assertIn("DocumentStats(tab)", body, f"{anchor} does not refresh the count")

    def test_typing_in_a_long_document_waits_for_a_pause(self):
        # A novel is millions of characters; recounting on every keystroke would
        # spend more time counting than writing.
        self.assertIn("var WORD_COUNT_FULL_SPEED_CHARS = 200000;", self.document)
        self.assertIn("var WORD_COUNT_LARGE_DELAY_MS = 600;", self.document)
        self.assertIn(
            "if ((tab.content || '').length <= WORD_COUNT_FULL_SPEED_CHARS) {",
            self.document,
        )

    def test_the_bar_is_chrome_and_stays_out_of_print_and_export(self):
        print_rules = self.document.split("@media print {", 1)[1].split("</style>", 1)[0]
        self.assertIn("#status-bar,", print_rules)
        self.assertIn("body { display: block; }", print_rules)

        exported = build_standalone_html("Notes", "<p>Hi</p>", ".markdown-body { color: black; }")
        self.assertNotIn("status-bar", exported)


if __name__ == "__main__":
    unittest.main()
