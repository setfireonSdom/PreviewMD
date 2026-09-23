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
        self.assertIn("document.getElementById('search-input').addEventListener('compositionstart', function() {", self.document)
        self.assertIn("document.getElementById('search-input').addEventListener('compositionend', function() {", self.document)
        self.assertIn("searchComposing = false;", self.document)

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
            "#tab-bar {\n    display: none;\n    height: 36px;\n    background: var(--toolbar-bg);",
            ".toolbar-sep {",
            '#view-modes button[aria-pressed="true"] {\n    background: var(--bg);',
            "#tab-image.editing-visible { display: inline-flex; }",
        ):
            self.assertIn(marker, self.document)
        for button_id in ("tab-add", "tab-image", "tab-toc", "tab-export"):
            tag, attributes = self.elements[button_id]
            self.assertEqual(tag, "button")
            self.assertIn("aria-label", attributes)
            self.assertTrue(attributes.get("title"))
        self.assertLess(self.document.index('id="tabs"'), self.document.index('id="tab-add"'))

    def test_dropzone_is_a_compact_centered_card(self):
        card_tag, _ = self.elements["dropzone-card"]
        self.assertEqual(card_tag, "div")
        for marker in (
            "#dropzone {\n    display: flex;\n    align-items: center;\n    justify-content: center;\n    min-height: 100%;",
            "#dropzone-card {\n    display: flex;\n    flex-direction: column;\n    align-items: center;\n    width: min(400px, calc(100% - 48px));",
            "#dropzone.drag-over #dropzone-card {",
            "background: var(--accent);\n    color: var(--accent-text);",
        ):
            self.assertIn(marker, self.document)


if __name__ == "__main__":
    unittest.main()
