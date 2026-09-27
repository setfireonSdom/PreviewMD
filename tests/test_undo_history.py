"""Undo history behaviour, driven through the real functions from the built page.

A string assertion can only prove that `trimUndoStack` exists. It cannot prove
that a 2.1 million character document keeps a usable history, which is exactly
the bug that let a flat 2 MB ceiling pin long novels to a single undo step
without saying anything. These tests extract the shipped functions and run
them, so a regression has to be deliberate.
"""

import json
import re
import shutil
import subprocess
import unittest

from preview import build_html

FUNCTIONS = (
    "undoBudgetChars",
    "undoStepCount",
    "undoLimitNotice",
    "trimUndoStack",
)

CONSTANTS = (
    "UNDO_STACK_LIMIT",
    "UNDO_STACK_MIN_CHARS",
    "UNDO_STACK_MAX_CHARS",
    "UNDO_STACK_DOCUMENT_MULTIPLE",
    "UNDO_STACK_MIN_ENTRIES",
)

# Stubs for the two UI functions trimUndoStack reaches for when it degrades.
PRELUDE = """
var notices = [];
function showStatus(text) { notices.push(text); }
function refreshTab() {}
"""

# Each case drives twelve editing pauses, the way a burst of typing does.
DRIVER = """
function simulate(length) {
    notices = [];
    var tab = { content: 'x'.repeat(length), undoStack: [], undoSteps: 0, undoDegraded: false };
    for (var k = 0; k < 12; k++) {
        tab.undoStack.push({ content: 'x'.repeat(length), start: 0, end: 0 });
        trimUndoStack(tab);
    }
    return { steps: tab.undoStack.length, degraded: tab.undoDegraded, notices: notices.length };
}
var sizes = __SIZES__;
var out = {};
sizes.forEach(function (length) { out[length] = simulate(length); });
console.log(JSON.stringify(out));
"""


def extract_undo_javascript() -> str:
    """Pull the undo helpers out of the generated page, verbatim."""
    document = build_html()
    parts = []
    for name in CONSTANTS:
        match = re.search(rf"var {name} = [^;]+;", document)
        if match is None:
            raise AssertionError(f"missing constant {name}")
        parts.append(match.group(0))
    for name in FUNCTIONS:
        match = re.search(rf"function {name}\(.*?\n\}}", document, re.S)
        if match is None:
            raise AssertionError(f"missing function {name}")
        parts.append(match.group(0))
    return "\n".join(parts)


@unittest.skipIf(shutil.which("node") is None, "node is required to run the undo logic")
class UndoHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sizes = [40_000, 350_000, 1_000_000, 1_706_746, 2_165_059, 21_000_000]
        # A token, not str.format: the driver is full of JavaScript braces.
        driver = PRELUDE + extract_undo_javascript() + "\n" + DRIVER.replace("__SIZES__", json.dumps(cls.sizes))
        result = subprocess.run(
            ["node", "-e", driver], capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        cls.results = {
            int(length): payload
            for length, payload in json.loads(result.stdout.strip()).items()
        }

    def test_every_document_size_keeps_a_usable_undo_history(self):
        # The invariant that matters: undo must always be able to reach past the
        # most recent edit. Documents within the size limit are never worse.
        for length, outcome in self.results.items():
            with self.subTest(characters=length):
                self.assertGreaterEqual(
                    outcome["steps"],
                    2,
                    f"a {length} character document is limited to {outcome['steps']} undo steps",
                )

    def test_long_novels_keep_several_steps_instead_of_one(self):
        # 2.1 million characters is the length of a 6.2 MB Chinese novel and sits
        # just past the old flat ceiling, which is why it was pinned to one step.
        for length in (1_706_746, 2_165_059):
            with self.subTest(characters=length):
                self.assertGreaterEqual(self.results[length]["steps"], 4)

    def test_small_documents_are_not_penalised(self):
        # A small document should never be told its history is limited.
        outcome = self.results[40_000]
        self.assertEqual(outcome["degraded"], False)
        self.assertEqual(outcome["notices"], 0)

    def test_limited_history_is_announced_instead_of_failing_silently(self):
        for length in (1_000_000, 2_165_059, 21_000_000):
            with self.subTest(characters=length):
                self.assertTrue(
                    self.results[length]["degraded"],
                    "losing undo history must set the degraded flag",
                )
                self.assertEqual(
                    self.results[length]["notices"],
                    1,
                    "the notice should appear once per tab, not once per edit",
                )

    def test_budget_scales_with_the_document_and_stays_capped(self):
        document = build_html()
        self.assertIn("var UNDO_STACK_DOCUMENT_MULTIPLE = 4;", document)
        self.assertIn("var UNDO_STACK_MIN_ENTRIES = 2;", document)
        # The old flat ceiling must be gone, not merely bypassed.
        self.assertNotIn("var UNDO_STACK_MAX_CHARS = 2 * 1024 * 1024;", document)

    def test_an_empty_undo_explains_itself(self):
        document = build_html()
        # Pressing undo with nothing left used to do nothing at all.
        self.assertIn("showStatus(tab.undoDegraded ? undoLimitNotice(tab) : 'Nothing to undo in this tab.');", document)
        self.assertIn("function undoLimitNotice(tab) {", document)

    def test_the_tab_tooltip_reports_the_limit(self):
        document = build_html()
        self.assertIn("', undo limited to ' + steps + ' step'", document)
        self.assertIn("if (tabState.undoDegraded) {", document)


if __name__ == "__main__":
    unittest.main()
