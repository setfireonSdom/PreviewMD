#!/usr/bin/env python3
"""
PreviewMD - A lightweight Markdown previewer for macOS.
Drag .md files into the window, or use File > Open.
Supports GFM, code highlighting, LaTeX math, multi-tab, and auto-refresh on file changes.
"""

import sys
import os
import json
import time
import hashlib

import webview
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ─── File watcher ────────────────────────────────────────────────────────────

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, filepath, callback):
        self.filepath = filepath
        self.callback = callback
        self._last = 0

    def on_modified(self, event):
        if event.is_directory:
            return
        if os.path.realpath(event.src_path) == os.path.realpath(self.filepath):
            now = time.time()
            if now - self._last > 0.3:
                self._last = now
                self.callback(self.filepath)


# ─── HTML template builder ───────────────────────────────────────────────────

def _read_resource(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_html():
    marked_js = _read_resource("marked.min.js")
    highlight_js = _read_resource("highlight.min.js")
    katex_js = _read_resource("katex.min.js")
    katex_css = _read_resource("katex.min.css")
    hl_css = _read_resource("github-dark.min.css")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*, *::before, *::after {{ box-sizing: border-box; }}

:root {{
    --bg: #ffffff;
    --text: #1a1a1a;
    --border: #e0e0e0;
    --accent: #0969da;
    --dropzone-bg: #f5f5f5;
    --dropzone-text: #888;
    --tab-active-bg: #ffffff;
    --tab-hover-bg: #e8e8e8;
    --tab-inactive-bg: #f0f0f0;
}}
@media (prefers-color-scheme: dark) {{
    :root {{
        --bg: #0d1117;
        --text: #c9d1d9;
        --border: #30363d;
        --accent: #58a6ff;
        --dropzone-bg: #161b22;
        --dropzone-text: #8b949e;
        --tab-active-bg: #0d1117;
        --tab-hover-bg: #1c2128;
        --tab-inactive-bg: #161b22;
    }}
}}

html, body {{
    margin: 0; padding: 0;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    overflow: hidden;
}}

/* ── Tab bar ── */
#tab-bar {{
    display: none;
    height: 36px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    padding: 0 4px;
    align-items: center;
    gap: 2px;
    user-select: none;
    -webkit-user-select: none;
    overflow-x: auto;
    overflow-y: hidden;
    white-space: nowrap;
}}
#tab-bar::-webkit-scrollbar {{ height: 0; }}
body.has-tabs #tab-bar {{ display: flex; }}

#tabs {{
    display: flex;
    align-items: center;
    gap: 2px;
    flex: 1;
    min-width: 0;
    height: 100%;
    padding-top: 6px;
}}

.tab {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 28px;
    padding: 0 8px;
    border-radius: 6px 6px 0 0;
    background: var(--tab-inactive-bg);
    border: 1px solid var(--border);
    border-bottom: none;
    cursor: pointer;
    font-size: 12px;
    color: var(--dropzone-text);
    max-width: 180px;
    flex-shrink: 0;
    transition: background 0.1s;
}}
.tab:hover {{
    background: var(--tab-hover-bg);
}}
.tab.active {{
    background: var(--tab-active-bg);
    color: var(--text);
    font-weight: 500;
}}

.tab-dot {{
    width: 6px; height: 6px;
    border-radius: 50%;
    background: transparent;
    flex-shrink: 0;
}}
.tab.active .tab-dot {{ background: #3fb950; }}
.tab-dot.changed {{ background: #d29922 !important; }}

.tab-name {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.tab-close {{
    width: 16px; height: 16px;
    border-radius: 3px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    line-height: 1;
    color: var(--dropzone-text);
    flex-shrink: 0;
    opacity: 0;
    transition: opacity 0.1s, background 0.1s;
}}
.tab:hover .tab-close {{ opacity: 1; }}
.tab-close:hover {{
    background: rgba(128,128,128,0.3);
    color: var(--text);
}}

#tab-add {{
    width: 24px; height: 24px;
    border-radius: 4px;
    border: none;
    background: transparent;
    color: var(--dropzone-text);
    font-size: 18px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    margin-right: 4px;
}}
#tab-add:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}

/* ── Main area ── */
#main-area {{
    height: calc(100% - 36px);
    overflow: hidden;
}}
body:not(.has-tabs) #main-area {{ height: 100%; }}

/* ── Drop zone ── */
#dropzone {{
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    gap: 12px;
    background: var(--dropzone-bg);
    border: 2px dashed var(--border);
    border-radius: 12px;
    margin: 40px;
    cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
}}
body.has-tabs #dropzone {{ display: none; }}

#dropzone.drag-over {{
    border-color: #58a6ff;
    background: rgba(88,166,255,0.08);
}}
#dropzone-icon {{
    font-size: 48px;
    opacity: 0.4;
}}
#dropzone p {{
    margin: 0;
    color: var(--dropzone-text);
    font-size: 15px;
}}
#dropzone button {{
    margin-top: 8px;
    padding: 8px 20px;
    font-size: 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
    cursor: pointer;
}}
#dropzone button:hover {{
    background: var(--dropzone-bg);
}}

/* ── Content area ── */
#content {{
    display: none;
    max-width: 900px;
    margin: 0 auto;
    padding: 32px 60px;
    height: 100%;
    overflow-y: auto;
    line-height: 1.6;
}}
body.has-tabs #content {{ display: block; }}

/* ── Markdown body styles ── */
.markdown-body {{
    font-size: 16px;
}}
.markdown-body h1 {{
    font-size: 2em;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.3em;
    margin-top: 0;
}}
.markdown-body h2 {{
    font-size: 1.5em;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.3em;
}}
.markdown-body h3 {{ font-size: 1.25em; }}
.markdown-body h4 {{ font-size: 1em; }}

.markdown-body code {{
    background: var(--dropzone-bg);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}}
.markdown-body pre {{
    background: var(--dropzone-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    overflow-x: auto;
    font-size: 14px;
    line-height: 1.5;
}}
.markdown-body pre code {{
    background: none;
    padding: 0;
    border-radius: 0;
    font-size: inherit;
}}

.markdown-body table {{
    border-collapse: collapse;
    width: 100%;
    margin: 16px 0;
}}
.markdown-body th, .markdown-body td {{
    border: 1px solid var(--border);
    padding: 8px 12px;
    text-align: left;
}}
.markdown-body th {{
    background: var(--dropzone-bg);
    font-weight: 600;
}}
.markdown-body blockquote {{
    border-left: 4px solid #58a6ff;
    margin: 0;
    padding: 0 16px;
    color: var(--dropzone-text);
}}
.markdown-body ul, .markdown-body ol {{
    padding-left: 2em;
}}
.markdown-body a {{
    color: #58a6ff;
    text-decoration: none;
}}
.markdown-body a:hover {{
    text-decoration: underline;
}}
.markdown-body img {{
    max-width: 100%;
}}
.markdown-body hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 24px 0;
}}
</style>
<style>
/* highlight.js theme */
{hl_css}
</style>
<style>
/* KaTeX */
{katex_css}
</style>
</head>
<body class="">

<div id="tab-bar">
  <div id="tabs"></div>
  <button id="tab-add" onclick="openNewFile()" title="Open .md file">&plus;</button>
</div>

<div id="main-area">

  <div id="dropzone">
    <div id="dropzone-icon">&downarrow;</div>
    <p>Drag a <strong>.md</strong> file here</p>
    <p>or</p>
    <button onclick="openNewFile()">Open File...</button>
  </div>

  <div id="content" class="markdown-body"></div>

</div>

<script>
// ── marked.js ──
{marked_js}
</script>

<script>
// ── highlight.js ──
{highlight_js}
</script>

<script>
// ── KaTeX ──
{katex_js}
</script>

<script>
// ── Configure marked ──
marked.setOptions({{
    gfm: true,
    breaks: false,
}});

const origCode = marked.Renderer.prototype.code;
marked.Renderer.prototype.code = function(code, language) {{
    if (language && hljs.getLanguage(language)) {{
        try {{
            const highlighted = hljs.highlight(code, {{ language: language, ignoreIllegals: true }}).value;
            return '<pre><code class="hljs language-' + language + '">' + highlighted + '</code></pre>';
        }} catch (e) {{}}
    }}
    try {{
        const highlighted = hljs.highlightAuto(code).value;
        return '<pre><code class="hljs">' + highlighted + '</code></pre>';
    }} catch (e) {{}}
    return origCode.call(this, code, language);
}};

// ── LaTeX rendering ──
function looksLikeMath(text) {{
    // Must contain at least one mathy character to avoid false positives
    // (e.g. "$...$" in normal text is not a formula)
    return /[a-zA-Z0-9\\\\^_{{}}+\\-\\=*\\/<>|]/.test(text);
}}

function renderMathInText(html) {{
    html = html.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(match, formula) {{
        if (!looksLikeMath(formula)) return match;
        try {{
            return katex.renderToString(formula.trim(), {{
                displayMode: true,
                throwOnError: false,
                trust: true
            }});
        }} catch (e) {{ return match; }}
    }});
    html = html.replace(/(?<!\\$)\\$(?!\\$)([^$]+?)\\$(?!\\$)/g, function(match, formula) {{
        if (match.charAt(0) === '\\\\') return match;
        if (!looksLikeMath(formula)) return match;
        try {{
            return katex.renderToString(formula.trim(), {{
                displayMode: false,
                throwOnError: false,
                trust: true
            }});
        }} catch (e) {{ return match; }}
    }});
    return html;
}}

// ── Tab state ──
var tabs = [];       // {{name, path, content}}
var activeIdx = -1;

function escapeHtml(text) {{
    var d = document.createElement('div');
    d.textContent = text;
    return d.innerHTML;
}}

function findTabByPath(path) {{
    if (!path) return -1;
    for (var i = 0; i < tabs.length; i++) {{
        if (tabs[i].path === path) return i;
    }}
    return -1;
}}

// ── Render markdown content ──
function renderContent(text) {{
    var html = marked.parse(text);
    html = renderMathInText(html);
    document.getElementById('content').innerHTML = html;
}}

// ── Render tab bar ──
function renderTabs() {{
    var container = document.getElementById('tabs');
    container.innerHTML = '';

    for (var i = 0; i < tabs.length; i++) {{
        (function(idx) {{
            var tab = document.createElement('div');
            tab.className = 'tab' + (idx === activeIdx ? ' active' : '');
            tab.onclick = function(e) {{
                if (e.target.classList.contains('tab-close')) return;
                switchTab(idx);
            }};

            var dot = document.createElement('span');
            dot.className = 'tab-dot';

            var name = document.createElement('span');
            name.className = 'tab-name';
            name.textContent = tabs[idx].name;

            var close = document.createElement('span');
            close.className = 'tab-close';
            close.innerHTML = '&times;';
            close.onclick = function(e) {{
                e.stopPropagation();
                closeTab(idx);
            }};

            tab.appendChild(dot);
            tab.appendChild(name);
            tab.appendChild(close);
            container.appendChild(tab);
        }})(i);
    }}
}}

// ── Render active tab content ──
function renderActiveTab() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    document.body.classList.add('has-tabs');
    renderContent(tabs[activeIdx].content);
}}

// ── Update window title ──
function syncWindowTitle() {{
    var title;
    if (activeIdx >= 0 && activeIdx < tabs.length) {{
        title = tabs[activeIdx].name + ' - PreviewMD';
    }} else {{
        title = 'PreviewMD';
    }}
    document.title = title;
    if (window.pywebview) {{
        window.pywebview.api.set_title(title);
    }}
}}

// ── Add a tab ──
function addTab(name, path, content) {{
    // If file is already open, just switch to it
    var existing = findTabByPath(path);
    if (existing >= 0) {{
        switchTab(existing);
        return;
    }}

    tabs.push({{ name: name, path: path, content: content }});
    activeIdx = tabs.length - 1;

    renderTabs();
    renderActiveTab();
    syncWindowTitle();
}}

// ── Close a tab ──
function closeTab(index) {{
    if (index < 0 || index >= tabs.length) return;

    var path = tabs[index].path;
    if (path && window.pywebview) {{
        window.pywebview.api.stop_watching(path);
    }}

    tabs.splice(index, 1);

    if (tabs.length === 0) {{
        activeIdx = -1;
        document.body.classList.remove('has-tabs');
        document.getElementById('content').innerHTML = '';
        syncWindowTitle();
        return;
    }}

    if (activeIdx >= tabs.length) activeIdx = tabs.length - 1;
    renderTabs();
    renderActiveTab();
    syncWindowTitle();
}}

// ── Switch to a tab ──
function switchTab(index) {{
    if (index < 0 || index >= tabs.length || index === activeIdx) return;
    activeIdx = index;
    renderTabs();
    renderActiveTab();
    syncWindowTitle();
}}

// ── Flash change indicator on active tab ──
function flashActiveDot() {{
    var tabs = document.querySelectorAll('.tab');
    if (activeIdx >= 0 && tabs[activeIdx]) {{
        var dot = tabs[activeIdx].querySelector('.tab-dot');
        dot.classList.add('changed');
        setTimeout(function() {{ dot.classList.remove('changed'); }}, 800);
    }}
}}

// ── Drop zone events ──
(function() {{
    var dz = document.getElementById('dropzone');

    dz.addEventListener('dragover', function(e) {{
        e.preventDefault();
        e.stopPropagation();
        dz.classList.add('drag-over');
    }});

    dz.addEventListener('dragleave', function(e) {{
        e.preventDefault();
        dz.classList.remove('drag-over');
    }});

    dz.addEventListener('drop', function(e) {{
        e.preventDefault();
        e.stopPropagation();
        dz.classList.remove('drag-over');
        handleFileDrop(e);
    }});
}})();

// ── Body-level drop (when tabs are open) ──
document.body.addEventListener('dragover', function(e) {{
    e.preventDefault();
    e.stopPropagation();
}});

document.body.addEventListener('drop', function(e) {{
    e.preventDefault();
    e.stopPropagation();
    handleFileDrop(e);
}});

function handleFileDrop(e) {{
    var file = e.dataTransfer.files[0];
    if (!file) return;

    var name = file.name || 'untitled.md';
    if (file.path && window.pywebview) {{
        window.pywebview.api.dropped_file(file.path);
    }}

    var reader = new FileReader();
    reader.onload = function(ev) {{
        addTab(name, file.path || null, ev.target.result);
    }};
    reader.readAsText(file);
}}

// ── Python bridge ──
function openNewFile() {{
    if (window.pywebview) {{
        window.pywebview.api.open_file();
    }}
}}

// Called from Python when a file is opened via dialog
window.addTabFromPython = function(name, path, content) {{
    addTab(name, path, content);
}};

// Called from Python when a watched file changes on disk
window.updateTabFromPython = function(path, content) {{
    var idx = findTabByPath(path);
    if (idx < 0) return;
    tabs[idx].content = content;
    if (idx === activeIdx) {{
        renderContent(content);
        flashActiveDot();
    }}
}};

// Called from Python when a drag-drop path is available
window.openPathFromPython = function(name, path, content) {{
    addTab(name, path, content);
}};
</script>

</body>
</html>"""


# ─── Application ─────────────────────────────────────────────────────────────

class Api:
    """JS-Python bridge exposed to the webview."""

    def __init__(self, app):
        self._app = app

    def open_file(self):
        result = self._app._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Markdown Files (*.md)",)
        )
        if result and result[0]:
            self._app.load_file(result[0])

    def dropped_file(self, filepath):
        """Called from JS when drag-drop provides a file path."""
        if filepath and os.path.exists(filepath):
            self._app.load_file(filepath)

    def stop_watching(self, filepath):
        """Called from JS when a tab is closed."""
        self._app.unwatch_file(filepath)

    def set_title(self, title):
        self._app._window.set_title(title)


class PreviewApp:
    def __init__(self):
        self._window = None
        self._watchers = {}     # {filepath: {"observer": ..., "md5": ...}}
        self._pending_file = None
        self._api = Api(self)

    def _hash_content(self, text):
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def load_file(self, filepath):
        """Read a .md file, send to JS as a new tab, and start watching."""
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            return

        filename = os.path.basename(filepath)
        escaped_name = json.dumps(filename)
        escaped_path = json.dumps(filepath)
        escaped_content = json.dumps(content)

        # Stop previous watcher for this path if any
        self.unwatch_file(filepath)

        # Start watching
        self._start_watching(filepath)

        # Send to JS
        self._window.evaluate_js(
            f"window.addTabFromPython({escaped_name}, {escaped_path}, {escaped_content})"
        )

    def _start_watching(self, filepath):
        watched_dir = os.path.dirname(filepath)
        handler = FileChangeHandler(filepath, callback=self._on_file_changed)
        observer = Observer()
        observer.schedule(handler, watched_dir, recursive=False)
        observer.start()

        self._watchers[filepath] = {
            "observer": observer,
            "md5": None,
        }

    def unwatch_file(self, filepath):
        if filepath in self._watchers:
            self._watchers[filepath]["observer"].stop()
            self._watchers[filepath]["observer"].join(timeout=1)
            del self._watchers[filepath]

    def _on_file_changed(self, filepath):
        """Called by file watcher when a watched file is modified."""
        if filepath not in self._watchers:
            return

        time.sleep(0.15)

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return

        new_md5 = self._hash_content(content)
        old_md5 = self._watchers[filepath]["md5"]
        if new_md5 == old_md5:
            return
        self._watchers[filepath]["md5"] = new_md5

        escaped_path = json.dumps(filepath)
        escaped_content = json.dumps(content)
        self._window.evaluate_js(
            f"window.updateTabFromPython({escaped_path}, {escaped_content})"
        )

    def _on_loaded(self):
        """Handle initial file from CLI, or a file:// URL from drag-drop interception."""
        try:
            url = self._window.get_current_url()
            if url and url.startswith("file://"):
                filepath = url.replace("file://", "")
                if os.path.exists(filepath) and filepath.endswith(".md"):
                    self.load_file(filepath)
        except Exception:
            pass

        if self._pending_file:
            pending = self._pending_file
            self._pending_file = None
            self.load_file(pending)

    def run(self, filepath=None):
        self._pending_file = filepath
        html = build_html()
        self._window = webview.create_window(
            "PreviewMD",
            html=html,
            js_api=self._api,
            width=1100,
            height=850,
            min_size=(600, 400),
            text_select=True,
        )
        self._window.events.loaded += self._on_loaded

        webview.start(debug=False)
        self._cleanup()

    def _cleanup(self):
        for path in list(self._watchers.keys()):
            self.unwatch_file(path)


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    filepath = sys.argv[1] if len(sys.argv) > 1 else None
    if filepath and not os.path.exists(filepath):
        print(f"File not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    app = PreviewApp()
    app.run(filepath)


if __name__ == "__main__":
    main()
