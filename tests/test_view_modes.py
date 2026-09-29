import re
import unittest
from html.parser import HTMLParser

from preview import build_html


class ElementCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.elements[element_id] = (tag, attributes)


class ViewModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_html()
        parser = ElementCollector()
        parser.feed(cls.document)
        cls.elements = parser.elements

    def test_three_accessible_mode_buttons_default_to_preview(self):
        expected_states = {
            "mode-preview": "true",
            "mode-edit": "false",
            "mode-split": "false",
        }
        for element_id, pressed in expected_states.items():
            tag, attributes = self.elements[element_id]
            self.assertEqual(tag, "button")
            self.assertEqual(attributes["type"], "button")
            self.assertEqual(attributes["aria-pressed"], pressed)
            self.assertIn("aria-label", attributes)

        group_tag, group_attributes = self.elements["view-modes"]
        self.assertEqual(group_tag, "div")
        self.assertEqual(group_attributes["role"], "group")
        self.assertEqual(group_attributes["aria-label"], "View mode")

    def test_css_defines_preview_edit_split_and_print_layouts(self):
        for rule in (
            "body.split-mode #editor-wrap { display: block; flex: 1 1 50%; }",
            "body.split-mode #content { flex: 1 1 50%; }",
            "body.edit-mode #editor-wrap { display: block; flex: 1 1 auto; border-right: 0; }",
            "body.edit-mode #content { display: none; }",
            "body.split-mode #workspace { flex-wrap: wrap; }",
            "#content { display: block !important; height: auto; overflow: visible; padding: 0; color: #111; }",
        ):
            self.assertIn(rule, self.document)

    def test_view_capabilities_are_separate_and_centrally_switched(self):
        for marker in (
            "var viewMode = 'preview';",
            "function hasEditor()",
            "function hasPreview()",
            "function isSplitView()",
            "function setViewMode(nextMode)",
            "document.body.classList.toggle('edit-mode', viewMode === 'edit')",
            "document.body.classList.toggle('split-mode', viewMode === 'split')",
            "document.getElementById('tab-image').classList.toggle('editing-visible', hasEditor())",
            "document.getElementById('tab-toc').disabled = !hasPreview()",
        ):
            self.assertIn(marker, self.document)

        for obsolete_marker in ("isEditing", "toggleEdit", "enterEditMode", "exitEditMode"):
            self.assertNotIn(obsolete_marker, self.document)

    def test_live_render_and_scroll_sync_are_split_only(self):
        self.assertIn("if (isSplitView()) {", self.document)
        self.assertIn("var scrollSyncSource = null;", self.document)
        self.assertIn("if (!isSplitView()) return;", self.document)
        self.assertIn("if (this === scrollSyncSource) { scrollSyncSource = null; return; }", self.document)
        self.assertIn("if (!hasEditor() || activeIdx < 0 || activeIdx >= tabs.length) return;", self.document)
        self.assertIn("return hasEditor() && context && context.editorSession", self.document)

    def test_export_can_refresh_a_hidden_preview(self):
        self.assertIn("if (!state || state.tabId !== tab.id || state.content !== tab.content)", self.document)
        self.assertIn("renderContent(tab.content, tab.path, true, tab);", self.document)
        self.assertIn("await awaitStableExportRender(tab)", self.document)

    def test_search_and_close_do_not_silently_destroy_edits(self):
        self.assertIn("document.getElementById('search-input').focus({ preventScroll: true });", self.document)
        self.assertIn("if (tab.dirty && !window.confirm", self.document)
        self.assertIn("could not be fully saved", self.document)

    def test_search_jumps_the_editor_to_the_match(self):
        self.assertIn("function measureEditorOffset(editor, position)", self.document)
        self.assertIn("editor.scrollTop = Math.max(0, matchOffset - editor.clientHeight / 2);", self.document)
        self.assertIn("selectEditorMatch(searchMatches[0].start, searchMatches[0].end);", self.document)
        self.assertNotIn("editor.focus();\n    editor.setSelectionRange(start, end);", self.document)

    def test_search_pauses_editor_jumps_during_ime_composition(self):
        self.assertIn("if (searchMatches.length > 0 && !searchComposing) {", self.document)
        self.assertIn("if (e.isComposing) return;", self.document)
        self.assertIn(
            "document.getElementById('search-input').addEventListener('compositionstart', function() {",
            self.document,
        )
        self.assertIn(
            "document.getElementById('search-input').addEventListener('compositionend', function() {",
            self.document,
        )
        self.assertIn("searchComposing = false;", self.document)

    def test_macos_tab_shortcuts_are_handled_in_the_page(self):
        for marker in (
            "if (meta && !e.shiftKey && key === 'w') {",
            "if (activeIdx >= 0) closeTab(activeIdx);",
            "if (meta && !e.shiftKey && /^[1-9]$/.test(e.key)) {",
            "if (tabIndex < tabs.length) switchTab(tabIndex);",
            "if (meta && e.shiftKey && key === 't') {",
            "reopenClosedTab();",
        ):
            self.assertIn(marker, self.document)

    def test_closed_tabs_can_be_reopened_through_python(self):
        for marker in (
            "function rememberClosedTab(tab) {",
            "async function reopenClosedTab() {",
            "rememberClosedTab(tab);",
            "window.closeActiveTab = function() {",
            "callBridge('reopen_path', entry.path);",
        ):
            self.assertIn(marker, self.document)

    def test_editor_search_paints_highlights_behind_the_textarea(self):
        layer_tag, layer_attributes = self.elements["editor-highlights"]
        self.assertEqual(layer_tag, "div")
        self.assertEqual(layer_attributes["aria-hidden"], "true")
        for rule in (
            "#editor-stack {\n    position: relative;\n    width: 100%;\n    height: 100%;\n}",
            "#editor-highlights mark {\n    background: #ffe066;\n    color: transparent;\n    border-radius: 2px;\n}",
            "#editor-highlights mark.current {\n    background: #d9a54d;\n}",
            "background: transparent;\n    color: var(--text);",
            "position: relative;\n    z-index: 1;",
        ):
            self.assertIn(rule, self.document)
        for marker in (
            "function renderEditorHighlights()",
            "function syncEditorHighlightScroll()",
            "layer.scrollTop = editor.scrollTop;",
            "searchMatches = collectEditorMatches(editor.value, query);",
        ):
            self.assertIn(marker, self.document)

    def test_toolbar_uses_compact_native_styling(self):
        for marker in (
            "--toolbar-bg: #eceae5;",
            "--focus-ring: rgba(125, 143, 128, .40);",
            # The bar is a flex item of the window column, and it is pinned:
            # unpinned it shrinks by its share of a line whose workspace is as
            # tall as the document, which squeezed it flat on long pages.
            "#tab-bar {\n    display: none;\n    height: 36px;\n    /* Pinned: as a flex item",
            "flex: 0 0 36px;",
            ".toolbar-sep {",
            '#view-modes button[aria-pressed="true"] {\n    background: var(--bg);',
            "#tab-image.editing-visible { display: inline-flex; }",
        ):
            self.assertIn(marker, self.document)
        self.assertNotIn("body:not(.has-tabs) #main-area { height: 100%; }", self.document)
        for button_id in ("tab-add", "tab-image", "tab-toc", "tab-export"):
            tag, attributes = self.elements[button_id]
            self.assertEqual(tag, "button")
            self.assertIn("aria-label", attributes)
            self.assertTrue(attributes.get("title"))
        self.assertLess(self.document.index('id="tabs"'), self.document.index('id="tab-add"'))

    def test_every_toolbar_icon_is_given_a_size(self):
        # An svg with only a viewBox and no width collapses to 0x0 and paints
        # nothing, so the reading size button was an empty rounded rectangle: the
        # sizing rule listed every other icon by id and had forgotten this one.
        # A new toolbar button must not be able to repeat that.
        style = self.document[self.document.index("<style>"): self.document.index("</style>")]
        # Comments carry braces and selectors, so they have to go before parsing.
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        sized = set()
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
            if "width:" not in match.group(2):
                continue
            for selector in match.group(1).split(","):
                selector = " ".join(selector.split())
                if selector.endswith(" svg") and selector.startswith("#"):
                    sized.add(selector)

        buttons = re.findall(r'<button id="(tab-[a-z-]+)"[^>]*>\s*<svg', self.document)
        self.assertGreaterEqual(len(buttons), 6)
        for button_id in buttons:
            self.assertIn(
                f"#{button_id} svg",
                sized,
                f"#{button_id} draws an svg but no rule gives it a width, so it "
                "renders as nothing",
            )

    def test_toolbar_menus_are_placed_where_the_user_can_see_them(self):
        # The reading size menu was absolutely positioned inside the tab bar, and
        # the tab bar scrolls horizontally with overflow-y hidden. That clipped
        # everything below the bar, and because the menu was placed with the
        # button's viewport coordinates the offset was counted twice, so it also
        # landed about 1200px to the right, outside the window. Clicking the
        # button did nothing visible. The export menu was already fixed and was
        # fine, which is why only one of the two broke.
        bar = self.document[self.document.index("#tab-bar {"):]
        bar = bar[: bar.index("}")]
        self.assertIn("overflow-y: hidden;", bar, "the premise: the bar clips")

        for menu_id in ("settings-menu", "export-menu"):
            block = self.document[self.document.index("#" + menu_id + " {"):]
            block = block[: block.index("}")]
            self.assertIn(
                "position: fixed;",
                block,
                f"#{menu_id} must be fixed: absolutely positioned it is clipped by "
                "the tab bar and offset by the button's coordinates",
            )
            self.assertNotIn("position: absolute;", block)

        # One placement routine for both, so the two cannot drift apart again.
        self.assertIn("function placeMenu(menu, anchor, width) {", self.document)
        self.assertIn("placeMenu(menu, button, menu.offsetWidth);", self.document)
        self.assertEqual(
            self.document.count("placeMenu(menu, button, menu.offsetWidth);"),
            2,
            "both menus must go through placeMenu",
        )
        # Viewport coordinates, and kept inside a narrow window.
        self.assertIn("var rect = anchor.getBoundingClientRect();", self.document)
        self.assertIn("window.innerWidth - width - margin", self.document)

        # The menu stays inside its wrapper, which is what the click-outside
        # handler tests. Moving it to the body would leave it open on every click.
        for wrap_id, menu_id in (("export-wrap", "export-menu"), ("settings-wrap", "settings-menu")):
            wrap = self.document[self.document.index('id="' + wrap_id + '"'):]
            wrap = wrap[: wrap.index("</div>")]
            self.assertIn('id="' + menu_id + '"', wrap)
        self.assertIn(
            "if (!document.getElementById('settings-wrap').contains(event.target)) closeSettingsMenu();",
            self.document,
        )

    def test_dropzone_is_a_compact_centered_card(self):
        card_tag, _ = self.elements["dropzone-card"]
        self.assertEqual(card_tag, "div")
        for marker in (
            (
                "#dropzone {\n    display: flex;\n    align-items: center;\n"
                "    justify-content: center;\n    min-height: 100%;"
            ),
            (
                "#dropzone-card {\n    display: flex;\n    flex-direction: column;\n"
                "    align-items: center;\n    width: min(400px, calc(100% - 48px));"
            ),
            "#dropzone.drag-over #dropzone-card {",
            "background: var(--accent);\n    color: var(--accent-text);",
        ):
            self.assertIn(marker, self.document)


if __name__ == "__main__":
    unittest.main()
