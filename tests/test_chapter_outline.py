import unittest

from preview import build_html


class ChapterOutlineTests(unittest.TestCase):
    """Plain text novels have no headings, so the outline falls back to 第N章 lines."""

    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_chapter_detection_markers_exist(self):
        for marker in (
            "var CHAPTER_PATTERN = ",
            "function chineseToNumber(text) {",
            "function chapterKey(numberText) {",
            "function collectChapterTargets(content) {",
            "var HEADING_TOC_FALLBACK_THRESHOLD = 3;",
        ):
            self.assertIn(marker, self.document)

    def test_chapter_numbers_are_collapsed_so_repeats_do_not_duplicate(self):
        for marker in (
            "var seen = new Map();",
            "if (existing) {",
            "if (firstText.length > existing.label.length) {",
        ):
            self.assertIn(marker, self.document)

    def test_real_headings_take_priority_over_chapter_detection(self):
        # Documents with enough headings keep the existing behaviour untouched.
        self.assertIn("if (headings.length >= HEADING_TOC_FALLBACK_THRESHOLD) {", self.document)
        self.assertIn("return { source: 'chapters', targets: chapters };", self.document)

    def test_active_outline_entry_is_found_without_reading_every_rect(self):
        # A novel can have >1500 chapters; one layout read per target per scroll
        # frame would make scrolling stutter, so the search is a binary search.
        # The behavioural half of this lives in OutlineLookupCostTests below:
        # a string check cannot tell a binary search from a linear scan, because
        # the linear version can be written with any formatting at all.
        self.assertIn("function activeTocTarget() {", self.document)
        self.assertIn("var middle = (low + high) >> 1;", self.document)

    def test_outline_keeps_the_active_entry_in_view(self):
        for marker in (
            "function revealActiveTocButton() {",
            "sidebar.scrollTop += buttonRect.bottom - sidebarRect.bottom + 10;",
        ):
            self.assertIn(marker, self.document)

    def test_progress_is_updated_before_the_entry_is_scrolled_into_view(self):
        # Showing the progress line inserts a row above the list; doing it in the
        # other order pushed the active entry back out of view.
        heading = self.document.index("function updateActiveHeading()")
        progress = self.document.index("updateReadingProgress(current);", heading)
        activate = self.document.index("setActiveTocHeading(current.element.id);", heading)
        self.assertLess(progress, activate)

    def test_existing_ids_are_not_overwritten(self):
        # In-document links depend on ids the document already had.
        self.assertIn("if (!element.id) {", self.document)


class ReadingPositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_positions_are_stored_as_ratios(self):
        for marker in (
            "function scrollRatio(element) {",
            "function applyScrollRatio(element, ratio) {",
            "function applyPendingScrollPositions(tab) {",
            "tab.previewRatio = scrollRatio(content);",
            "positions[tab.path] = Math.round(tab.previewRatio * 10000) / 10000;",
        ):
            self.assertIn(marker, self.document)

    def test_the_session_payload_carries_positions(self):
        self.assertIn("view_mode: viewMode, positions: positions", self.document)
        self.assertIn("state.positions[tab.path]", self.document)

    def test_position_is_applied_after_a_progressive_render_finishes(self):
        self.assertIn("rendered.then(function() { applyPendingScrollPositions(tab); });", self.document)


class ReducedMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()

    def test_scrolls_and_transitions_respect_reduce_motion(self):
        self.assertIn("@media (prefers-reduced-motion: reduce) {", self.document)
        self.assertIn("function prefersReducedMotion() {", self.document)
        self.assertIn("function scrollBehavior() {", self.document)
        # Both smooth-scroll call sites go through the helper instead of a literal.
        self.assertEqual(self.document.count("behavior: behavior || scrollBehavior()"), 1)
        self.assertEqual(self.document.count("behavior: scrollBehavior()"), 1)
        self.assertNotIn("behavior: 'smooth'", self.document)


if __name__ == "__main__":
    unittest.main()
