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
import base64
import binascii
import html
import re
import stat
import tempfile
import threading
import unicodedata
import webbrowser
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import webview
from webview.dom import DOMEventHandler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024
IMAGE_CACHE_MAX_ENTRIES = 64
WATCHER_POLL_INTERVAL_SECONDS = 2.0
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
CANONICAL_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
CSS_URL_PATTERN = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)


class SaveConflictError(RuntimeError):
    """The file changed after the caller's last known disk version."""

    def __init__(self, disk_hash):
        super().__init__("File changed on disk.")
        self.disk_hash = disk_hash


class ImageError(ValueError):
    """An image cannot be safely imported or rendered."""


class ImagePathRequiredError(ImageError):
    """The document needs a location on disk before images can be stored."""


def detect_image_mime(data):
    """Return the supported image MIME identified by magic bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sanitize_image_basename(filename, mime=None):
    """Keep useful Unicode while removing path/control and unsafe filename chars."""
    raw_name = unicodedata.normalize("NFC", os.path.basename(str(filename or "image")))
    stem, supplied_ext = os.path.splitext(raw_name)
    cleaned = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
        for char in stem
    ).strip(" .")
    cleaned = cleaned or "image"
    extension = None
    if mime is not None:
        supplied_lower = supplied_ext.lower()
        extension = (
            supplied_lower
            if IMAGE_TYPES.get(supplied_lower) == mime
            else CANONICAL_IMAGE_EXTENSIONS.get(mime)
        )
    if extension is None:
        extension = supplied_ext.lower()
        if extension not in IMAGE_TYPES:
            raise ImageError("Only PNG, JPEG, GIF, and WebP images are supported.")
    return cleaned + extension


def _validated_image_bytes(data, filename="image"):
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("Image is larger than the 20 MB limit.")
    mime = detect_image_mime(data)
    if mime is None:
        raise ImageError("Only PNG, JPEG, GIF, and WebP images are supported.")
    supplied_ext = os.path.splitext(str(filename))[1].lower()
    if supplied_ext and supplied_ext not in IMAGE_TYPES:
        raise ImageError("Only PNG, JPEG, GIF, and WebP images are supported.")
    return mime


def import_image_bytes(document_path, filename, data):
    """Store image bytes beside a path-backed document and return Markdown path."""
    if not document_path:
        raise ImageError("This tab has no file path; image import is unavailable.")
    mime = _validated_image_bytes(data, filename)
    safe_name = sanitize_image_basename(filename, mime)
    assets_dir = Path(document_path).resolve().parent / "assets"
    assets_dir.mkdir(exist_ok=True)
    stem, extension = os.path.splitext(safe_name)
    candidate_number = 1
    while True:
        candidate_name = safe_name if candidate_number == 1 else f"{stem}-{candidate_number}{extension}"
        candidate_path = assets_dir / candidate_name
        try:
            with candidate_path.open("xb") as output:
                output.write(data)
            return PurePosixPath("assets", candidate_name).as_posix()
        except FileExistsError:
            candidate_number += 1


def import_image_file(document_path, source_path):
    """Import a supported file without ever copying it into application storage."""
    if not document_path:
        raise ImageError("This tab has no file path; image import is unavailable.")
    source = Path(source_path)
    try:
        size = source.stat().st_size
    except OSError as error:
        raise ImageError(f"Could not read image: {error}") from error
    if size > MAX_IMAGE_BYTES:
        raise ImageError("Image is larger than the 20 MB limit.")
    try:
        data = source.read_bytes()
    except OSError as error:
        raise ImageError(f"Could not read image: {error}") from error
    return import_image_bytes(document_path, source.name, data)


def resolve_local_image_path(document_path, source):
    """Resolve a local Markdown image source for a path-backed document."""
    if not document_path or not source:
        return None
    parsed = urlparse(str(source).strip())
    if parsed.scheme.lower() in ("http", "https", "data"):
        return None
    if parsed.scheme and parsed.scheme.lower() != "file":
        return None
    if parsed.scheme.lower() == "file" and parsed.netloc not in ("", "localhost"):
        return None
    decoded_path = unquote(parsed.path)
    if not decoded_path:
        return None
    candidate = Path(decoded_path)
    if not candidate.is_absolute():
        candidate = Path(document_path).resolve().parent / candidate
    try:
        candidate = candidate.resolve()
        if candidate.suffix.lower() not in IMAGE_TYPES or not candidate.is_file():
            return None
        if candidate.stat().st_size > MAX_IMAGE_BYTES:
            return None
    except OSError:
        return None
    return candidate


def image_data_url(image_path):
    """Encode an already-resolved local image as a bridge-safe data URL, or None."""
    try:
        data = image_path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    mime = detect_image_mime(data)
    if mime is None or IMAGE_TYPES.get(image_path.suffix.lower()) != mime:
        # .jpg and .jpeg both map to image/jpeg, while spoofed files are rejected.
        return None
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def local_image_data_url(document_path, source):
    """Return a supported local image as a bridge-safe data URL, or None."""
    image_path = resolve_local_image_path(document_path, source)
    if image_path is None:
        return None
    return image_data_url(image_path)


def safe_export_filename(title):
    """Return a native-dialog suggestion, never an export destination."""
    stem = os.path.splitext(os.path.basename(str(title or "document")))[0]
    cleaned = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
        for char in stem
    ).strip(" .")
    return f"{cleaned or 'document'}.html"


def safe_save_filename(title, default_stem="Untitled"):
    """Return a safe Save As suggestion that keeps a Markdown/text extension."""
    raw_name = unicodedata.normalize("NFC", os.path.basename(str(title or "")))
    stem, extension = os.path.splitext(raw_name)
    if extension.lower() not in (".md", ".txt"):
        extension = ".md"
    cleaned = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
        for char in stem
    ).strip(" .")
    return f"{cleaned or default_stem}{extension}"


def ensure_document_extension(filepath):
    """Give a user-selected Save As path a Markdown extension if it has none."""
    value = str(filepath)
    if value.lower().endswith((".md", ".txt")):
        return value
    return f"{value.rstrip(' .') or value}.md"


def display_path(filepath):
    """Return a user-facing path with the home directory abbreviated."""
    value = str(filepath or "")
    home = os.path.expanduser("~")
    if home and (value == home or value.startswith(home + os.sep)):
        return f"~{value[len(home):]}"
    return value


def build_standalone_html(title, rendered_html, stylesheet):
    """Build a complete UTF-8 document from already-sanitized rendered markup."""
    escaped_title = html.escape(str(title or "PreviewMD Export"), quote=True)
    safe_stylesheet = str(stylesheet or "").replace("</style", "<\\/style")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>{safe_stylesheet}</style>
</head>
<body><main class="markdown-body">{rendered_html}</main></body>
</html>
"""


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_utf8_with_hash(filepath):
    with open(filepath, "r", encoding="utf-8") as source:
        content = source.read()
    return content, content_hash(content)


def watched_file_signature(filepath):
    """Return a cheap change fingerprint for polling; None when unreadable."""
    try:
        file_stat = os.stat(filepath)
    except OSError:
        return None
    return (file_stat.st_mtime_ns, file_stat.st_size)


def atomic_write_utf8(filepath, content, expected_hash=None, force=False, replace=os.replace):
    """Optimistically and atomically replace a UTF-8 text file in its directory."""
    filepath = os.path.realpath(os.path.abspath(filepath))
    directory = os.path.dirname(filepath)
    try:
        original_mode = stat.S_IMODE(os.stat(filepath).st_mode)
        _, current_hash = read_utf8_with_hash(filepath)
    except FileNotFoundError:
        if not force:
            raise SaveConflictError(None)
        original_mode = 0o644
        current_hash = None
    if not force and expected_hash != current_hash:
        raise SaveConflictError(current_hash)

    descriptor, temporary_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(filepath)}.", suffix=".tmp", dir=directory
    )
    try:
        os.fchmod(descriptor, original_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

        # Recheck immediately before replacement so a concurrent edit is not
        # knowingly overwritten by an autosave.
        if not force:
            try:
                _, latest_hash = read_utf8_with_hash(filepath)
            except FileNotFoundError:
                latest_hash = None
            if latest_hash != current_hash:
                raise SaveConflictError(latest_hash)
        replace(temporary_path, filepath)
        temporary_path = None
        try:
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
        return content_hash(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def atomic_export_utf8(filepath, content, replace=os.replace):
    """Atomically create or replace an explicitly user-selected export path."""
    return atomic_write_utf8(filepath, content, force=True, replace=replace)


# ─── Session persistence ─────────────────────────────────────────────────────

SESSION_VERSION = 1
SESSION_VIEW_MODES = ("preview", "edit", "split")


def session_file_path(env=None):
    """Return the per-user session file, overridable for tests and packaging."""
    environment = os.environ if env is None else env
    override = environment.get("PREVIEWMD_SESSION_FILE")
    if override:
        return os.path.realpath(os.path.abspath(os.path.expanduser(override)))
    if sys.platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support" / "PreviewMD"
    else:
        state_home = environment.get("XDG_STATE_HOME")
        base_dir = Path(state_home) if state_home else Path.home() / ".local" / "state"
        base_dir = base_dir / "PreviewMD"
    return os.fspath(base_dir / "session.json")


def normalize_session(payload):
    """Reduce arbitrary input to an openable tab list plus the active index."""
    session = {
        "version": SESSION_VERSION,
        "tabs": [],
        "active": 0,
        "view_mode": "preview",
        "last_directory": "",
    }
    if not isinstance(payload, dict):
        return session
    raw_tabs = payload.get("tabs")
    if isinstance(raw_tabs, list):
        for item in raw_tabs:
            if not isinstance(item, str) or not item:
                continue
            path = os.path.realpath(os.path.abspath(os.path.expanduser(item)))
            if not path.lower().endswith((".md", ".txt")) or path in session["tabs"]:
                continue
            session["tabs"].append(path)
    try:
        session["active"] = max(0, int(payload.get("active", 0)))
    except (TypeError, ValueError):
        session["active"] = 0
    if payload.get("view_mode") in SESSION_VIEW_MODES:
        session["view_mode"] = payload["view_mode"]
    raw_directory = payload.get("last_directory")
    if isinstance(raw_directory, str) and raw_directory.strip():
        session["last_directory"] = os.path.realpath(
            os.path.abspath(os.path.expanduser(raw_directory))
        )
    return session


def load_session(filepath=None):
    filepath = filepath or session_file_path()
    try:
        with open(filepath, "r", encoding="utf-8") as source:
            return normalize_session(json.load(source))
    except (OSError, ValueError):
        return normalize_session(None)


def persist_session(state, filepath=None):
    """Atomically store the session; session IO must never break editing."""
    filepath = filepath or session_file_path()
    document = json.dumps(normalize_session(state), ensure_ascii=False, indent=2)
    try:
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)
        atomic_export_utf8(filepath, document)
        return True
    except OSError as error:
        print(f"Could not store the session: {error}", file=sys.stderr)
        return False


def plan_session_restore(paths, active, exists=os.path.exists):
    """Return (restorable paths, active index) after dropping missing files."""
    restored = []
    target = 0
    for index, path in enumerate(paths):
        if not exists(path):
            continue
        if index <= active:
            target = len(restored)
        restored.append(path)
    return restored, target


def inline_css_resources(stylesheet, resource_root=None):
    """Inline safe KaTeX font URLs and neutralize unavailable fallbacks."""
    if resource_root is None:
        base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        resource_root = os.path.join(base_dir, "resources")
    resource_root = Path(resource_root).resolve()

    def replace_url(match):
        raw_path = match.group(2).strip()
        if raw_path.lower().startswith("data:"):
            return match.group(0)
        parsed = urlparse(raw_path)
        pure_path = PurePosixPath(parsed.path)
        is_safe_font = (
            not parsed.scheme
            and not parsed.netloc
            and not pure_path.is_absolute()
            and ".." not in pure_path.parts
            and len(pure_path.parts) == 2
            and pure_path.parts[0] == "fonts"
            and pure_path.suffix.lower() in (".woff2", ".woff", ".ttf")
        )
        if not is_safe_font:
            raise ValueError(f"Unsafe CSS resource URL: {raw_path}")
        font_path = (resource_root / Path(*pure_path.parts)).resolve()
        if resource_root not in font_path.parents:
            raise ValueError(f"CSS resource escapes resource directory: {raw_path}")
        if not font_path.is_file():
            if pure_path.suffix.lower() == ".woff2":
                raise FileNotFoundError(f"Required KaTeX font is missing: {raw_path}")
            return 'url("data:application/octet-stream;base64,")'
        mime = "font/woff2" if pure_path.suffix.lower() == ".woff2" else "application/octet-stream"
        encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        return f'url("data:{mime};base64,{encoded}")'

    return CSS_URL_PATTERN.sub(replace_url, stylesheet)


# ─── File watcher ────────────────────────────────────────────────────────────

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, filepath, token, callback):
        self.filepath = filepath
        self.token = token
        self.callback = callback
        self._last = 0

    def _handle_path(self, changed_path):
        if os.path.realpath(changed_path) == os.path.realpath(self.filepath):
            now = time.time()
            if now - self._last > 0.3:
                self._last = now
                self.callback(self.filepath, self.token)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle_path(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._handle_path(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle_path(event.dest_path)


# ─── HTML template builder ───────────────────────────────────────────────────

def _read_resource(filename):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base_dir, "resources", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_html():
    marked_js = _read_resource("marked.min.js")
    highlight_js = _read_resource("highlight.min.js")
    katex_js = _read_resource("katex.min.js")
    katex_css = inline_css_resources(_read_resource("katex.min.css"))
    hl_css = _read_resource("highlight.css")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*, *::before, *::after {{ box-sizing: border-box; }}

:root {{
    --bg: #faf8f2;
    --text: #37342e;
    --border: #d8d3c9;
    --accent: #7d8f80;
    --accent-soft: rgba(125, 143, 128, .15);
    --accent-text: #f6f4ef;
    --link: #5f7a5c;
    --dropzone-bg: #e2dfd8;
    --dropzone-text: #6f6a61;
    --tab-active-bg: #faf8f2;
    --tab-hover-bg: rgba(42, 38, 29, .07);
    --tab-inactive-bg: #e2dfd8;
    --toolbar-bg: #eceae5;
    --segmented-shadow: 0 1px 2px rgba(28, 25, 20, .10), 0 0 0 1px rgba(28, 25, 20, .08);
    --focus-ring: rgba(125, 143, 128, .40);
    --status-saved: #5f7a5c;
    --status-changed: #7d8f80;
    --danger: #a84a38;
    --danger-soft: rgba(168, 74, 56, .18);
    --warning: #a37b3f;
}}
@media (prefers-color-scheme: dark) {{
    :root {{
        --bg: #211f1c;
        --text: #d6d2c8;
        --border: rgba(216, 211, 201, .14);
        --accent: #8fa392;
        --accent-soft: rgba(143, 163, 146, .18);
        --accent-text: #1f1e1a;
        --link: #a3b8a6;
        --dropzone-bg: rgba(216, 211, 201, .07);
        --dropzone-text: #9b968c;
        --tab-active-bg: #38362f;
        --tab-hover-bg: rgba(216, 211, 201, .08);
        --tab-inactive-bg: rgba(216, 211, 201, .12);
        --toolbar-bg: #2b2925;
        --segmented-shadow: 0 1px 2px rgba(0, 0, 0, .5), 0 0 0 1px rgba(255, 255, 255, .06);
        --focus-ring: rgba(143, 163, 146, .45);
        --status-saved: #87a37e;
        --status-changed: #8fa392;
        --danger: #c4796a;
        --danger-soft: rgba(196, 121, 106, .22);
        --warning: #c29b5e;
    }}
}}

html, body {{
    margin: 0; padding: 0;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
}}

/* ── Tab bar ── */
#tab-bar {{
    display: none;
    height: 36px;
    background: var(--toolbar-bg);
    border-bottom: 1px solid var(--border);
    padding: 0 8px;
    align-items: center;
    gap: 6px;
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
    gap: 3px;
    flex: 1;
    min-width: 0;
    height: 100%;
}}

.tab {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 26px;
    padding: 0 9px;
    border: none;
    border-radius: 7px;
    background: transparent;
    cursor: pointer;
    font-size: 12px;
    color: var(--dropzone-text);
    max-width: 180px;
    flex-shrink: 0;
    transition: background 0.1s, box-shadow 0.1s, color 0.1s;
}}
.tab:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}
.tab.active {{
    background: var(--tab-active-bg);
    color: var(--text);
    font-weight: 500;
    box-shadow: var(--segmented-shadow);
}}

.tab-dot {{
    width: 6px; height: 6px;
    border-radius: 50%;
    background: transparent;
    flex-shrink: 0;
}}
.tab.saved.active .tab-dot {{ background: var(--status-saved); }}
.tab.dirty .tab-dot {{ background: var(--warning); }}
.tab.conflict .tab-dot {{ background: var(--danger); box-shadow: 0 0 0 2px var(--danger-soft); }}
.tab.externally-changed:not(.dirty):not(.conflict) .tab-dot {{ background: var(--status-changed); }}
.tab-dot.changed {{ animation: tab-pulse .8s ease; }}
@keyframes tab-pulse {{ 50% {{ transform: scale(1.7); }} }}

.tab-name {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.tab-close {{
    width: 16px; height: 16px;
    padding: 0;
    border: none;
    background: transparent;
    border-radius: 3px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    line-height: 1;
    color: var(--dropzone-text);
    flex-shrink: 0;
    opacity: 0;
    cursor: pointer;
    transition: opacity 0.1s, background 0.1s;
}}
.tab:hover .tab-close {{ opacity: 1; }}
.tab-close:focus-visible {{ opacity: 1; }}
.tab-close:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}

#tab-add, #tab-new {{
    width: 26px; height: 26px;
    border-radius: 6px;
    border: none;
    background: transparent;
    color: var(--dropzone-text);
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
}}
#tab-add:hover, #tab-new:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}

#tab-image, #tab-reload, #tab-toc, #tab-export {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    height: 26px;
    padding: 0 9px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.1s, color 0.1s;
}}
#tab-image {{ display: none; }}
#tab-image.editing-visible {{ display: inline-flex; }}
#tab-image:hover, #tab-reload:hover, #tab-toc:hover, #tab-export:hover {{
    background: var(--tab-hover-bg);
}}
#tab-image:disabled, #tab-reload:disabled, #tab-toc:disabled, #tab-export:disabled {{
    opacity: .5;
    cursor: default;
}}
#tab-image svg, #tab-reload svg, #tab-toc svg, #tab-export svg {{
    width: 15px; height: 15px;
    flex-shrink: 0;
}}
#tab-add svg, #tab-new svg {{
    width: 14px; height: 14px;
    flex-shrink: 0;
}}

.toolbar-sep {{
    width: 1px;
    height: 18px;
    background: var(--border);
    flex-shrink: 0;
    margin: 0 2px;
}}

#view-modes {{
    display: inline-flex;
    align-items: center;
    gap: 2px;
    padding: 2px;
    background: var(--tab-inactive-bg);
    border-radius: 7px;
    flex-shrink: 0;
}}
#view-modes button {{
    height: 22px;
    padding: 0 10px;
    border: none;
    border-radius: 5px;
    background: transparent;
    color: var(--dropzone-text);
    font-size: 12px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.1s, color 0.1s, box-shadow 0.1s;
}}
#view-modes button:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}
#view-modes button[aria-pressed="true"] {{
    background: var(--bg);
    color: var(--text);
    font-weight: 600;
    box-shadow: var(--segmented-shadow);
}}
#view-modes button:disabled {{
    opacity: .5;
    cursor: default;
}}

#export-wrap {{ position: relative; flex-shrink: 0; }}
#export-menu {{
    display: none;
    position: fixed;
    z-index: 60;
    min-width: 190px;
    padding: 4px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg);
    box-shadow: 0 8px 24px rgba(0,0,0,.22);
}}
#export-menu.open {{ display: block; }}
#export-menu button {{
    display: block; width: 100%; border: 0; border-radius: 5px;
    padding: 6px 10px; text-align: left; background: transparent;
    color: var(--text); font: inherit; font-size: 12px; cursor: pointer;
}}
#export-menu button + button {{
    margin-top: 4px;
    border-top: 1px solid var(--border);
    border-radius: 0 0 5px 5px;
    padding-top: 8px;
}}
#export-menu button:hover, #export-menu button:focus {{ background: var(--tab-hover-bg); }}

button:focus-visible, [role="button"]:focus-visible, input:focus-visible,
textarea:focus-visible, .tab:focus-visible {{
    outline: 3px solid var(--focus-ring);
    outline-offset: 1px;
}}

/* ── Editor textarea ── */
#editor-wrap {{
    display: none;
    min-width: 0;
    height: 100%;
    padding: 12px clamp(12px, 3vw, 32px);
    box-sizing: border-box;
    border-right: 1px solid var(--border);
}}
#editor-stack {{
    position: relative;
    width: 100%;
    height: 100%;
}}
#editor-highlights {{
    position: absolute;
    inset: 0;
    overflow: hidden;
    pointer-events: none;
    color: transparent;
    z-index: 0;
}}
#editor-highlights mark {{
    background: #ffe066;
    color: transparent;
    border-radius: 2px;
}}
#editor-highlights mark.current {{
    background: #d9a54d;
}}
/* Engine-painted matches for the editor mirror; no per-match DOM nodes. */
::highlight(previewmd-editor-search) {{
    background: #ffe066;
}}
::highlight(previewmd-editor-search-current) {{
    background: #d9a54d;
}}
@media (prefers-color-scheme: dark) {{
    #editor-highlights mark {{
        background: rgba(255, 224, 102, .30);
        box-shadow: inset 0 0 0 1px rgba(255, 224, 102, .55);
    }}
    #editor-highlights mark.current {{
        background: rgba(255, 224, 102, .34);
        box-shadow: inset 0 0 0 2px #ffe066;
    }}
    ::highlight(previewmd-editor-search) {{
        background: rgba(255, 224, 102, .30);
    }}
    ::highlight(previewmd-editor-search-current) {{
        background: rgba(255, 224, 102, .34);
    }}
}}
#editor {{
    width: 100%;
    height: 100%;
    border: none;
    outline: none;
    resize: none;
    background: transparent;
    color: var(--text);
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 15px;
    line-height: 1.7;
    padding: 16px 0;
    box-sizing: border-box;
    tab-size: 4;
    position: relative;
    z-index: 1;
}}

/* ── Search bar ── */
#search-bar {{
    display: none;
    position: absolute;
    top: 12px;
    right: clamp(8px, 2vw, 20px);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 4px 8px;
    z-index: 10;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}}
.search-row {{
    display: flex;
    align-items: center;
    gap: 6px;
}}
#search-input {{
    width: 200px;
    border: none;
    outline: none;
    background: transparent;
    color: var(--text);
    font-size: 13px;
    font-family: inherit;
    padding: 2px 4px;
}}
#search-count {{
    font-size: 12px;
    color: var(--dropzone-text);
    min-width: 36px;
    text-align: center;
    white-space: nowrap;
}}
#search-prev, #search-next, #search-close {{
    width: 22px; height: 22px;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--dropzone-text);
    font-size: 14px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}}
#search-prev:hover, #search-next:hover, #search-close:hover {{
    background: var(--tab-hover-bg);
    color: var(--text);
}}

/* ── Disk conflict banner ── */
#conflict-banner {{
    display: none;
    position: absolute;
    top: 12px;
    left: 50%;
    transform: translateX(-50%);
    align-items: center;
    gap: 8px;
    max-width: min(600px, calc(100% - 24px));
    padding: 7px 10px 7px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg);
    color: var(--text);
    font-size: 12px;
    z-index: 12;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
}}
#conflict-banner.visible {{ display: flex; }}
#conflict-banner-text {{ flex: 1 1 auto; }}
#conflict-banner button {{
    flex-shrink: 0;
    height: 22px;
    padding: 0 9px;
    border: none;
    border-radius: 5px;
    background: var(--accent-soft);
    color: var(--text);
    font-size: 12px;
    cursor: pointer;
}}
#conflict-banner button:hover {{ background: var(--accent); color: var(--accent-text); }}

/* ── Search highlights ── */
mark.search-highlight {{
    background: #ffe066;
    color: #2a261d;
    border-radius: 2px;
    padding: 0 1px;
}}
mark.search-highlight.current {{
    background: #d9a54d;
    color: #2a261d;
    outline: 2px solid #d9a54d;
}}
@media (prefers-color-scheme: dark) {{
    mark.search-highlight {{
        background: rgba(255, 224, 102, .35);
        color: inherit;
    }}
    mark.search-highlight.current {{
        background: #c9a44e;
        color: #211f1c;
        outline-color: #c9a44e;
    }}
}}

/* Custom Highlight API variant: matches are painted by the engine from ranges,
   so a huge number of hits never materializes DOM nodes. */
::highlight(previewmd-search) {{
    background: #ffe066;
    color: #2a261d;
}}
::highlight(previewmd-search-current) {{
    background: #d9a54d;
    color: #2a261d;
}}
@media (prefers-color-scheme: dark) {{
    ::highlight(previewmd-search) {{
        background: rgba(255, 224, 102, .35);
        color: inherit;
    }}
    ::highlight(previewmd-search-current) {{
        background: #c9a44e;
        color: #211f1c;
    }}
}}

/* ── Status toast ── */
#status-toast {{
    position: absolute;
    left: 50%;
    bottom: 18px;
    transform: translateX(-50%) translateY(8px);
    max-width: min(520px, calc(100% - 40px));
    padding: 7px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--bg);
    color: var(--text);
    font-size: 12px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.18);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.16s ease, transform 0.16s ease;
    z-index: 20;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
#status-toast.visible {{
    opacity: 1;
    transform: translateX(-50%) translateY(0);
}}

/* ── Main area ── */
#main-area {{
    position: relative;
    height: calc(100% - 36px);
    overflow: hidden;
}}
body:not(.has-tabs) #main-area {{ height: 100%; }}

#workspace {{ display: none; width: 100%; height: 100%; min-width: 0; }}
body.has-tabs #workspace {{ display: flex; }}

#toc-sidebar {{
    display: none;
    width: 220px;
    flex: 0 0 220px;
    height: 100%;
    overflow-y: auto;
    padding: 14px 10px;
    border-right: 1px solid var(--border);
    background: var(--bg);
    z-index: 15;
}}
body.toc-open #toc-sidebar {{ display: block; }}
#toc-title {{ margin: 0 8px 9px; font-size: 12px; font-weight: 650; color: var(--dropzone-text); }}
#toc-list {{ list-style: none; padding: 0; margin: 0; }}
#toc-list button {{
    display: block; width: 100%; border: 0; border-radius: 4px;
    padding: 5px 8px; background: transparent; color: var(--dropzone-text);
    text-align: left; font: inherit; font-size: 12px; line-height: 1.3;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer;
}}
#toc-list button:hover {{ background: var(--tab-hover-bg); color: var(--text); }}
#toc-list button.active {{ color: var(--accent); background: var(--accent-soft); font-weight: 600; }}
#toc-empty {{ padding: 6px 8px; color: var(--dropzone-text); font-size: 12px; }}

/* ── Drop zone ── */
#dropzone {{
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100%;
    cursor: pointer;
}}
body.has-tabs #dropzone {{ display: none; }}

#dropzone-card {{
    display: flex;
    flex-direction: column;
    align-items: center;
    width: min(400px, calc(100% - 48px));
    gap: 8px;
    padding: 32px 36px;
    border: 1.5px dashed var(--border);
    border-radius: 16px;
    transition: border-color 0.2s, background 0.2s;
}}
#dropzone.drag-over #dropzone-card {{
    border-color: var(--accent);
    background: var(--accent-soft);
}}
#dropzone-icon {{
    display: flex;
    color: var(--dropzone-text);
    opacity: 0.75;
}}
#dropzone-icon svg {{
    width: 28px; height: 28px;
}}
#dropzone-title {{
    margin: 0;
    color: var(--text);
    font-size: 14px;
}}
#dropzone-hint {{
    margin: 0 0 8px;
    color: var(--dropzone-text);
    font-size: 12px;
}}
#dropzone button {{
    padding: 7px 16px;
    font-size: 13px;
    font-weight: 500;
    border: none;
    border-radius: 7px;
    background: var(--accent);
    color: var(--accent-text);
    cursor: pointer;
    transition: opacity 0.1s;
}}
#dropzone button:hover {{
    opacity: 0.9;
}}
#dropzone-actions {{
    display: flex;
    align-items: center;
    gap: 8px;
}}
#dropzone button.secondary {{
    background: transparent;
    color: var(--link);
    box-shadow: inset 0 0 0 1px var(--border);
}}
#dropzone button.secondary:hover {{
    opacity: 1;
    background: var(--tab-hover-bg);
}}

/* ── Content area ── */
#content {{
    display: block;
    flex: 1 1 auto;
    min-width: 0;
    padding: clamp(20px, 4vw, 32px) clamp(16px, 6vw, 60px);
    height: 100%;
    overflow-y: auto;
    line-height: 1.6;
}}
body.split-mode #editor-wrap {{ display: block; flex: 1 1 50%; }}
body.split-mode #content {{ flex: 1 1 50%; }}
body.edit-mode #editor-wrap {{ display: block; flex: 1 1 auto; border-right: 0; }}
body.edit-mode #content {{ display: none; }}
#content > * {{ max-width: 900px; margin-left: auto; margin-right: auto; }}

/* ── Markdown body styles ── */
.markdown-body {{
    font-size: 16px;
    overflow-wrap: break-word;
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
    background: var(--inline-code-bg);
    color: var(--inline-code-text);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 0.9em;
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}}
.markdown-body pre {{
    background: var(--code-bg);
    border: 1px solid var(--code-border);
    border-radius: 6px;
    padding: 16px;
    overflow-x: auto;
    font-size: 14px;
    line-height: 1.5;
}}
.markdown-body pre code {{
    background: transparent;
    color: var(--code-text);
    padding: 0;
    border-radius: 0;
    font-size: inherit;
}}

.markdown-body table {{
    border-collapse: collapse;
    display: block;
    width: 100%;
    overflow-x: auto;
    margin: 16px auto;
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
    border-left: 3px solid var(--accent);
    margin: 0;
    padding: 0 16px;
    color: var(--dropzone-text);
}}
.markdown-body ul, .markdown-body ol {{
    padding-left: 2em;
}}
.markdown-body a {{
    color: var(--link);
    text-decoration: none;
    overflow-wrap: anywhere;
}}
.markdown-body a:hover {{
    text-decoration: underline;
}}
.markdown-body img {{
    display: block;
    max-width: 100%;
    max-height: min(75vh, 900px);
    width: auto;
    height: auto;
    object-fit: contain;
    margin: 1.25em auto;
    border-radius: 8px;
    cursor: zoom-in;
}}
.markdown-body img.image-loading {{
    min-width: 80px;
    min-height: 40px;
    background: var(--dropzone-bg);
}}
.markdown-body img.image-broken {{
    min-width: 180px;
    min-height: 48px;
    padding: 12px;
    border: 1px dashed #cf222e;
    background: var(--dropzone-bg);
    cursor: default;
}}
.markdown-body .task-list-item {{ list-style: none; }}
.markdown-body input.task-list-item-checkbox[type="checkbox"] {{
    margin: 0 0.45em 0.2em -1.4em;
    vertical-align: middle;
    accent-color: var(--accent);
}}
.markdown-body .katex-display {{
    max-width: 100%;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 0.2em 0;
}}
.markdown-body hr {{
    border: none;
    border-top: 1px solid var(--border);
    margin: 24px 0;
}}

/* ── Image lightbox ── */
#lightbox {{
    display: none; position: fixed; inset: 0; z-index: 100;
    background: rgba(0, 0, 0, .55); color: #fff;
    -webkit-backdrop-filter: blur(25px) saturate(140%);
    backdrop-filter: blur(25px) saturate(140%);
    align-items: center; justify-content: center; flex-direction: column;
    padding: 54px 24px 24px;
}}
#lightbox.open {{ display: flex; }}
#lightbox-image-wrap {{ max-width: 100%; max-height: calc(100% - 48px); overflow: auto; }}
#lightbox-image {{ display: block; max-width: none; max-height: none; transform-origin: center; }}
#lightbox-caption {{ max-width: 80vw; margin-top: 10px; font-size: 13px; color: rgba(255, 255, 255, .75); text-align: center; }}
#lightbox-controls {{ position: absolute; top: 12px; right: 14px; display: flex; gap: 6px; }}
#lightbox-controls button {{
    min-width: 34px; height: 32px; border: none; border-radius: 8px;
    background: rgba(255, 255, 255, .16); color: #fff; font-size: 15px; cursor: pointer;
    transition: background 0.1s;
}}
#lightbox-controls button:hover {{ background: rgba(255, 255, 255, .26); }}

@media (max-width: 760px) {{
    #tab-image, #tab-reload, #tab-toc, #tab-export {{ padding: 0 7px; }}
    #view-modes button {{ padding: 0 7px; }}
    body.split-mode #workspace {{ flex-wrap: wrap; }}
    body.split-mode #editor-wrap, body.split-mode #content {{ flex: 1 1 100%; height: 50%; }}
    body.split-mode #editor-wrap {{ border-right: 0; border-bottom: 1px solid var(--border); }}
    #toc-sidebar {{ position: absolute; left: 0; top: 0; width: min(260px, 72vw); box-shadow: 5px 0 18px rgba(0,0,0,.2); }}
}}

@media print {{
    html, body {{ height: auto; overflow: visible; background: #fff; color: #111; }}
    #tab-bar, #dropzone, #editor-wrap, #toc-sidebar, #search-bar, #conflict-banner,
    #status-toast, #lightbox {{ display: none !important; }}
    #main-area, #workspace {{ display: block !important; height: auto; overflow: visible; }}
    #content {{ display: block !important; height: auto; overflow: visible; padding: 0; color: #111; }}
    #content > * {{ max-width: none; }}
    .markdown-body a {{ color: #111; text-decoration: underline; }}
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
  <button id="tab-add" onclick="openNewFile()" title="Open Markdown or text file" aria-label="Open file">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9"/></svg>
  </button>
  <button id="tab-new" onclick="newUntitledTab()" title="New document (Cmd+N)" aria-label="New document">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.5 2.5h-5A1.5 1.5 0 0 0 3 4v8a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 12V6z"/><path d="M9.5 2.5V6H13"/><path d="M8 8.5v3M6.5 10h3"/></svg>
  </button>
  <button id="tab-image" onclick="pickImage()" title="Insert image" aria-label="Insert image">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="5.5" cy="6.5" r="1.1"/><path d="M3.5 11.5l3-2.8 2.2 2 1.8-1.6 2 1.8"/></svg>
  </button>
  <button id="tab-reload" onclick="reloadActiveTab()" title="Reload from disk (Cmd+R)" aria-label="Reload from disk">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>
  </button>
  <button id="tab-toc" onclick="toggleToc()" title="Toggle table of contents" aria-label="Toggle table of contents" aria-expanded="false">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true"><path d="M6 4h7M6 8h7M6 12h5"/><path d="M3 4h.01M3 8h.01M3 12h.01" stroke-width="1.8"/></svg>
  </button>
  <div id="export-wrap">
    <button id="tab-export" onclick="toggleExportMenu(event)" title="Save, export, or print" aria-label="Save, export, or print" aria-haspopup="menu" aria-expanded="false">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 10V2.5M5.5 5 8 2.5 10.5 5"/><path d="M3 9.5v3A1.5 1.5 0 0 0 4.5 14h7a1.5 1.5 0 0 0 1.5-1.5v-3"/></svg>
    </button>
    <div id="export-menu" role="menu">
      <button type="button" role="menuitem" onclick="saveFileAs()">Save As...</button>
      <button type="button" role="menuitem" onclick="exportHtml()">Export HTML...</button>
      <button type="button" role="menuitem" onclick="printDocument()">Print / Save as PDF...</button>
    </div>
  </div>
  <div class="toolbar-sep" aria-hidden="true"></div>
  <div id="view-modes" role="group" aria-label="View mode">
    <button id="mode-preview" type="button" onclick="setViewMode('preview')" title="Show preview only" aria-label="Preview only" aria-pressed="true">Preview</button>
    <button id="mode-edit" type="button" onclick="setViewMode('edit')" title="Show editor only" aria-label="Edit only" aria-pressed="false">Edit</button>
    <button id="mode-split" type="button" onclick="setViewMode('split')" title="Show editor and preview" aria-label="Split editor and preview" aria-pressed="false">Split</button>
  </div>
</div>

<div id="main-area">

  <div id="search-bar">
    <div class="search-row">
      <input id="search-input" type="text" placeholder="Find in page..." aria-label="Search current document" autofocus>
      <span id="search-count">0/0</span>
      <button id="search-prev" title="Previous match" aria-label="Previous match">&uarr;</button>
      <button id="search-next" title="Next match" aria-label="Next match">&darr;</button>
      <button id="search-close" title="Close search" aria-label="Close search">&times;</button>
    </div>
  </div>

  <div id="conflict-banner" role="status" aria-live="polite">
    <span id="conflict-banner-text">This file changed on disk. Autosave is paused.</span>
    <button id="conflict-reload" type="button" onclick="reloadActiveTab()">Load disk version</button>
    <button id="conflict-keep" type="button" onclick="keepLocalEdits()">Keep my edits</button>
  </div>

  <div id="dropzone">
    <div id="dropzone-card">
      <div id="dropzone-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M12 11v6M9.5 14.5 12 17l2.5-2.5"/></svg>
      </div>
      <p id="dropzone-title">Drag a <strong>.md</strong> or <strong>.txt</strong> file here</p>
      <p id="dropzone-hint">or</p>
      <div id="dropzone-actions">
        <button class="secondary" onclick="newUntitledTab()">New File...</button>
        <button onclick="openNewFile()">Open File...</button>
      </div>
    </div>
  </div>

  <div id="workspace">
    <nav id="toc-sidebar" aria-label="Table of contents">
      <div id="toc-title">Table of contents</div>
      <ol id="toc-list"></ol>
      <div id="toc-empty">No headings</div>
    </nav>
    <div id="editor-wrap">
      <div id="editor-stack">
        <div id="editor-highlights" aria-hidden="true"></div>
        <textarea id="editor" spellcheck="false" aria-label="Markdown editor"></textarea>
      </div>
    </div>
    <div id="content" class="markdown-body" tabindex="-1"></div>
  </div>

  <div id="status-toast" role="status" aria-live="polite"></div>

</div>

<div id="lightbox" role="dialog" aria-modal="true" aria-label="Image preview" aria-hidden="true">
  <div id="lightbox-controls">
    <button id="lightbox-minus" type="button" aria-label="Zoom out">&minus;</button>
    <button id="lightbox-reset" type="button" aria-label="Reset zoom">100%</button>
    <button id="lightbox-plus" type="button" aria-label="Zoom in">+</button>
    <button id="lightbox-close" type="button" aria-label="Close image preview">&times;</button>
  </div>
  <div id="lightbox-image-wrap"><img id="lightbox-image" alt=""></div>
  <div id="lightbox-caption"></div>
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
// Auto-detection scans every registered grammar, which is far too slow on long
// documents, so unlabelled code blocks are matched against a short list.
var HIGHLIGHT_AUTO_LANGUAGES = [
    'bash', 'c', 'cpp', 'csharp', 'css', 'diff', 'go', 'ini', 'java', 'javascript',
    'json', 'kotlin', 'lua', 'makefile', 'markdown', 'objectivec', 'perl', 'php',
    'python', 'ruby', 'rust', 'scss', 'shell', 'sql', 'swift', 'typescript', 'xml', 'yaml'
];
marked.use({{
    gfm: true,
    breaks: true,
    renderer: {{
        code(token) {{
            if (token.lang && hljs.getLanguage(token.lang)) {{
                try {{
                    const highlighted = hljs.highlight(token.text, {{ language: token.lang, ignoreIllegals: true }}).value;
                    return '<pre><code class="hljs language-' + token.lang + '">' + highlighted + '</code></pre>';
                }} catch (e) {{}}
            }}
            try {{
                const highlighted = hljs.highlightAuto(token.text, HIGHLIGHT_AUTO_LANGUAGES).value;
                return '<pre><code class="hljs">' + highlighted + '</code></pre>';
            }} catch (e) {{}}
            return false;
        }}
    }}
}});

// ── Safe HTML and LaTeX rendering ──
var statusTimer = null;
var ESCAPED_DOLLAR_TOKEN = '@@PREVIEWMD_ESCAPED_DOLLAR@@';

function showStatus(message) {{
    var toast = document.getElementById('status-toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('visible');
    clearTimeout(statusTimer);
    statusTimer = setTimeout(function() {{
        toast.classList.remove('visible');
    }}, 2200);
}}

function callBridge(method) {{
    var args = Array.prototype.slice.call(arguments, 1);
    if (!window.pywebview || !window.pywebview.api || typeof window.pywebview.api[method] !== 'function') {{
        return Promise.reject(new Error('Native bridge is unavailable.'));
    }}
    try {{
        return Promise.resolve(window.pywebview.api[method].apply(window.pywebview.api, args));
    }} catch (error) {{
        return Promise.reject(error);
    }}
}}

function isSafeUrl(value) {{
    var trimmed = (value || '').trim();
    // SECURITY: ASCII_CONTROL_NORMALIZATION prevents obfuscated schemes.
    // Ordinary relative filenames keep their spaces; only this probe is compacted.
    var schemeProbe = trimmed.replace(/[\\u0000-\\u001f\\u007f]+/g, '').toLowerCase();
    if (!trimmed || trimmed.charAt(0) === '#') return true;
    if (/^data:image\\/(png|jpeg|gif|webp);base64,[a-z0-9+/=\\s]+$/i.test(trimmed)) return true;
    if (/^(https?|mailto|file):/.test(schemeProbe)) return true;
    if (/^[a-z][a-z0-9+.-]*:/i.test(schemeProbe)) return false;
    return true;
}}

function isSafeTaskCheckbox(el) {{
    if (el.tagName !== 'INPUT') return false;
    if ((el.getAttribute('type') || '').toLowerCase() !== 'checkbox' || !el.hasAttribute('disabled')) return false;
    var allowed = new Set(['type', 'disabled', 'checked', 'class']);
    var safeClass = !el.hasAttribute('class') || el.getAttribute('class') === 'task-list-item-checkbox';
    return safeClass && Array.from(el.attributes).every(function(attr) {{
        return allowed.has(attr.name.toLowerCase());
    }});
}}

// Sanitize a parsed DOM in place: no serialization round trip, so a long
// document is parsed exactly once per render.
function sanitizeFragment(root) {{
    var blockedTags = new Set([
        'SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE',
        'FORM', 'BUTTON', 'TEXTAREA', 'SELECT', 'OPTION'
    ]);

    root.querySelectorAll('*').forEach(function(el) {{
        if (blockedTags.has(el.tagName)) {{
            el.remove();
            return;
        }}
        if (el.tagName === 'INPUT' && !isSafeTaskCheckbox(el)) {{
            el.remove();
            return;
        }}

        Array.from(el.attributes).forEach(function(attr) {{
            var name = attr.name.toLowerCase();
            var value = attr.value || '';
            var isUrlAttribute = name === 'href' || name === 'src';
            if (name.indexOf('on') === 0 || name === 'srcdoc' || name === 'style') {{
                el.removeAttribute(attr.name);
            }} else if (name === 'srcset') {{
                el.removeAttribute(attr.name);
            }} else if (isUrlAttribute && !isSafeUrl(value)) {{
                el.removeAttribute(attr.name);
            }}
        }});

        if (el.tagName === 'A') {{
            el.setAttribute('rel', 'noopener noreferrer');
        }}
    }});
}}

function protectEscapedDollars(text) {{
    return text.split('\\\\$').join(ESCAPED_DOLLAR_TOKEN);
}}

function restoreEscapedDollars(value) {{
    return value.split(ESCAPED_DOLLAR_TOKEN).join('$');
}}

function restoreEscapedDollarsInElement(root) {{
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) {{
        if (walker.currentNode.nodeValue.indexOf(ESCAPED_DOLLAR_TOKEN) >= 0) nodes.push(walker.currentNode);
    }}
    nodes.forEach(function(node) {{ node.nodeValue = restoreEscapedDollars(node.nodeValue); }});
}}

function looksLikeMath(text) {{
    var trimmed = text.trim();
    if (!trimmed) return false;
    if (/[\\\\^{{}}_]/.test(trimmed)) return true;
    if (/[A-Za-z]\\s*[=+\\-*\\/<>]|[=+\\-*\\/<>]\\s*[A-Za-z]/.test(trimmed)) return true;
    return /\\d\\s*[=+\\-*\\/<>]\\s*\\d/.test(trimmed);
}}

function renderFormula(formula, displayMode) {{
    if (!looksLikeMath(formula)) return null;
    try {{
        return katex.renderToString(formula.trim(), {{
            displayMode: displayMode,
            throwOnError: false,
            trust: false,
            maxSize: 50,
            maxExpand: 1000
        }});
    }} catch (e) {{
        return null;
    }}
}}

function renderMathInText(text) {{
    var mathParts = [];
    var withDisplayMath = text.replace(/\\$\\$([\\s\\S]*?)\\$\\$/g, function(match, formula) {{
        var rendered = renderFormula(formula, true);
        if (!rendered) return match;
        var token = '@@PREVIEWMD_MATH_' + mathParts.length + '@@';
        mathParts.push(rendered);
        return token;
    }});

    var withInlineMath = withDisplayMath.replace(/(^|[^$])\\$(?!\\$)([^$\\n]+?)\\$(?!\\$)/g, function(match, prefix, formula) {{
        var rendered = renderFormula(formula, false);
        if (!rendered) return match;
        var token = '@@PREVIEWMD_MATH_' + mathParts.length + '@@';
        mathParts.push(rendered);
        return prefix + token;
    }});

    var html = escapeHtml(withInlineMath);
    for (var i = 0; i < mathParts.length; i++) {{
        html = html.split('@@PREVIEWMD_MATH_' + i + '@@').join(mathParts[i]);
    }}
    return html;
}}

function hasBlockedAncestor(node, blockedTags, blockedClass) {{
    var parent = node.parentNode;
    while (parent && parent.nodeType === Node.ELEMENT_NODE) {{
        if (blockedTags.has(parent.tagName)) return true;
        if (blockedClass && parent.classList && parent.classList.contains(blockedClass)) return true;
        parent = parent.parentNode;
    }}
    return false;
}}

var MATH_BLOCKED_TAGS = new Set(['CODE', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'KBD', 'SAMP']);
// Every KaTeX formula costs roughly 0.4 ms to render, so a notes page with a few
// thousand formulas spends over a second before anything is visible. Render the
// first batch eagerly and the rest when their block scrolls near the viewport.
var MATH_EAGER_NODE_LIMIT = 150;
var pendingMathBlocks = new Map();
var mathObserver = null;
var mathEagerRendered = 0;

function renderMathTextNodes(nodes) {{
    nodes.forEach(function(node) {{
        if (!node || !node.parentNode) return;
        var rendered = renderMathInText(node.nodeValue);
        if (rendered === escapeHtml(node.nodeValue)) {{
            node.nodeValue = restoreEscapedDollars(node.nodeValue);
            return;
        }}
        var template = document.createElement('template');
        template.innerHTML = rendered;
        restoreEscapedDollarsInElement(template.content);
        node.parentNode.replaceChild(template.content, node);
    }});
}}

// Collect the text nodes that may contain LaTeX and restore escaped \$ everywhere
// else. Escaped dollars stay tokenized until after the math check so that "\$5"
// is never mistaken for a formula.
function prepareMathCandidates(root) {{
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var candidates = [];
    nodes.forEach(function(node) {{
        var value = node.nodeValue;
        if (!value) return;
        if (value.indexOf('$') >= 0 && !hasBlockedAncestor(node, MATH_BLOCKED_TAGS, 'katex')) {{
            candidates.push(node);
            return;
        }}
        if (value.indexOf(ESCAPED_DOLLAR_TOKEN) >= 0) node.nodeValue = restoreEscapedDollars(value);
    }});
    return candidates;
}}

function mathBlockFor(node, root) {{
    var element = node.parentNode;
    while (element && element !== root && element.parentNode !== root) {{
        element = element.parentNode;
    }}
    if (!element || element === root || element.parentNode !== root) return null;
    return element;
}}

function resetMathRendering() {{
    if (mathObserver) {{
        mathObserver.disconnect();
        mathObserver = null;
    }}
    pendingMathBlocks = new Map();
    mathEagerRendered = 0;
}}

function ensureMathObserver(contentElement) {{
    if (mathObserver) return mathObserver;
    mathObserver = new IntersectionObserver(function(entries) {{
        entries.forEach(function(entry) {{
            if (!entry.isIntersecting) return;
            var nodes = pendingMathBlocks.get(entry.target);
            if (!nodes) return;
            pendingMathBlocks.delete(entry.target);
            mathObserver.unobserve(entry.target);
            renderMathTextNodes(nodes);
        }});
    }}, {{ root: contentElement, rootMargin: '600px 0px' }});
    return mathObserver;
}}

// Called once per mounted batch, so progressive rendering can keep the eager
// budget document-wide while the rest waits for the viewport.
function scheduleMathRendering(candidates, contentElement) {{
    if (!candidates.length) return;
    var eager = [];
    var deferred = [];
    candidates.forEach(function(node) {{
        if (mathEagerRendered < MATH_EAGER_NODE_LIMIT) {{
            eager.push(node);
            mathEagerRendered += 1;
        }} else {{
            deferred.push(node);
        }}
    }});
    renderMathTextNodes(eager);
    if (!deferred.length) return;
    deferred.forEach(function(node) {{
        var block = mathBlockFor(node, contentElement);
        if (!block) {{ renderMathTextNodes([node]); return; }}
        if (!pendingMathBlocks.has(block)) pendingMathBlocks.set(block, []);
        pendingMathBlocks.get(block).push(node);
    }});
    if (!pendingMathBlocks.size) return;
    var observer = ensureMathObserver(contentElement);
    pendingMathBlocks.forEach(function(nodes, block) {{ observer.observe(block); }});
}}

// Export and print must contain every formula, not only the rendered ones.
function forceRenderAllMath() {{
    if (mathObserver) mathObserver.disconnect();
    mathObserver = null;
    var blocks = pendingMathBlocks;
    pendingMathBlocks = new Map();
    blocks.forEach(function(nodes) {{
        renderMathTextNodes(nodes.filter(function(node) {{ return node.parentNode; }}));
    }});
}}

// ── Chunked Markdown parsing ──
// JavaScriptCore's regex engine makes a single Markdown lexer pass over a long
// document superlinear (a 446 KB book can take ~10 s). Lexing the same text in
// blank-line separated chunks is linear and returns identical HTML, so large
// documents are parsed chunk by chunk.
var MARKDOWN_CHUNK_MAX_CHARS = 1000;
var LINK_DEFINITION_RE = /^ {{0,3}}\\[[^\\]]+\\]:/;

function markdownLineLooksLikeListItem(line) {{
    return /^\\s{{0,3}}([-*+]|\\d{{1,9}}[.)])(\\s|$)/.test(markdownLineWithoutQuotePrefix(line));
}}

function markdownLineIsIndented(line) {{
    return /^\\s{{2,}}\\S/.test(markdownLineWithoutQuotePrefix(line));
}}

function markdownLineContinuesList(line) {{
    return markdownLineLooksLikeListItem(line) || markdownLineIsIndented(line);
}}

function markdownLineWithoutQuotePrefix(line) {{
    return line.replace(/^\\s{{0,3}}> ?/, '');
}}

function markdownChunkBoundaryIsInsideList(lines, index) {{
    var previous = '';
    for (var i = index - 1; i >= 0; i--) {{
        if (lines[i].trim() !== '') {{ previous = lines[i]; break; }}
    }}
    var next = '';
    for (var j = index + 1; j < lines.length; j++) {{
        if (lines[j].trim() !== '') {{ next = lines[j]; break; }}
    }}
    return markdownLineContinuesList(previous) && markdownLineContinuesList(next);
}}

// Split Markdown into chunks on blank lines, never inside a fenced code block,
// a $$ display-math block, or a loose list. Link reference definitions are
// hoisted so they keep resolving document-wide after the split.
function splitMarkdownChunks(text) {{
    var lines = text.split('\\n');
    var chunks = [];
    var pending = [];
    var pendingChars = 0;
    var definitions = [];
    var fence = null;
    var inMathBlock = false;
    var block = [];

    function flushBlock() {{
        if (!block.length) return;
        var start = 0;
        while (start < block.length && LINK_DEFINITION_RE.test(block[start])) {{
            definitions.push(block[start]);
            start += 1;
        }}
        var body = block.slice(start);
        block = [];
        if (!body.length) return;
        var piece = body.join('\\n');
        pending.push(piece);
        pendingChars += piece.length + 2;
    }}

    function flushChunkIfFull() {{
        if (pendingChars >= MARKDOWN_CHUNK_MAX_CHARS && pending.length) {{
            chunks.push(pending.join('\\n\\n'));
            pending = [];
            pendingChars = 0;
        }}
    }}

    for (var i = 0; i < lines.length; i++) {{
        var line = lines[i];
        if (fence) {{
            block.push(line);
            var fenceClose = new RegExp('^\\s{{0,3}}' + fence.char + '{{' + fence.length + ',}}\\s*$');
            if (fenceClose.test(line)) fence = null;
            continue;
        }}
        var fenceOpen = line.match(/^\\s{{0,3}}(`{{3,}}|~{{3,}})/);
        var dollarCount = (line.match(/\\$\\$/g) || []).length;
        if (fenceOpen) {{
            block.push(line);
            fence = {{ char: fenceOpen[1].charAt(0), length: fenceOpen[1].length }};
            continue;
        }}
        if (inMathBlock) {{
            block.push(line);
            if (dollarCount % 2 === 1) inMathBlock = false;
            continue;
        }}
        if (dollarCount % 2 === 1) {{
            block.push(line);
            inMathBlock = true;
            continue;
        }}
        if (line === '') {{
            if (markdownChunkBoundaryIsInsideList(lines, i)) {{
                block.push(line);
                continue;
            }}
            flushBlock();
            flushChunkIfFull();
            continue;
        }}
        block.push(line);
    }}
    flushBlock();
    if (pending.length) chunks.push(pending.join('\\n\\n'));

    return {{ chunks: chunks, definitions: definitions }};
}}

// ── Tab state ──
var tabs = [];       // stable objects; async work must not rely on array indexes
var activeIdx = -1;
var renderGeneration = 0;
var nextTabId = 1;
var untitledCounter = 0;
var lastRenderState = null;
var liveRenderTimer = null;
// Live preview gets more patient as the document grows: a large document needs
// seconds to re-render, so re-triggering every 200 ms would never settle.
var LIVE_RENDER_BASE_MS = 200;
var LIVE_RENDER_MAX_MS = 1200;
var LIVE_RENDER_FULL_SPEED_CHARS = 200000;

function liveRenderDelay(text) {{
    var length = (text || '').length;
    var scale = Math.max(1, Math.min(LIVE_RENDER_MAX_MS / LIVE_RENDER_BASE_MS, length / LIVE_RENDER_FULL_SPEED_CHARS));
    return Math.min(LIVE_RENDER_MAX_MS, LIVE_RENDER_BASE_MS * scale);
}}
var activeTocId = null;
var tocButtons = new Map();
var lastNativeDirtyState = null;
window.previewmdNativeDropReady = false;

function hasUnsavedWork() {{
    return tabs.some(function(tab) {{ return tab.dirty || !!tab.savePromise; }});
}}

function syncNativeDirtyState() {{
    var dirty = hasUnsavedWork();
    if (dirty === lastNativeDirtyState) return;
    lastNativeDirtyState = dirty;
    callBridge('set_dirty_state', dirty).catch(function() {{
        // Retry on the next state synchronization if the bridge was not ready.
        lastNativeDirtyState = null;
    }});
}}

function sessionState() {{
    var paths = [];
    var active = 0;
    for (var i = 0; i < tabs.length; i++) {{
        if (!tabs[i].path) continue;
        if (i <= activeIdx) active = paths.length;
        paths.push(tabs[i].path);
    }}
    return {{ tabs: paths, active: active, view_mode: viewMode }};
}}

function syncSession() {{
    callBridge('save_session', sessionState()).catch(function() {{}});
}}

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
function markImageBroken(image, message) {{
    image.classList.remove('image-loading');
    image.classList.add('image-broken');
    image.removeAttribute('src');
    image.setAttribute('alt', (image.getAttribute('alt') || 'Image') + ' — unavailable');
    image.setAttribute('title', message || 'Image could not be loaded');
}}

function enableImageLightbox(image) {{
    if (image.dataset.lightboxReady === 'true') return;
    image.dataset.lightboxReady = 'true';
    image.addEventListener('click', function() {{
        if (!image.complete || image.naturalWidth <= 0 || image.classList.contains('image-broken')) return;
        openLightbox(image);
    }});
}}

function prepareImages(documentPath, generation, tab, root) {{
    var cache = tab ? tab.imageCache : null;
    var scope = root || document.getElementById('content');
    var jobs = Array.from(scope.querySelectorAll('img')).map(function(image) {{
        image.setAttribute('loading', 'lazy');
        image.setAttribute('decoding', 'async');
        image.addEventListener('error', function() {{
            if (generation === renderGeneration) markImageBroken(image, 'Image could not be loaded');
        }}, {{ once: true }});

        var source = image.getAttribute('src') || '';
        var isRemote = /^https?:\\/\\//i.test(source);
        var isSafeData = /^data:image\\/(png|jpeg|gif|webp);base64,/i.test(source);
        if (isRemote || isSafeData) {{
            enableImageLightbox(image);
            return Promise.resolve();
        }}
        image.removeAttribute('src');
        image.classList.add('image-loading');
        if (!source || !documentPath) {{
            markImageBroken(image, documentPath ? 'Unsupported image source' : 'Local images require a path-backed tab');
            return Promise.resolve();
        }}
        if (cache && Object.prototype.hasOwnProperty.call(cache, source)) {{
            image.classList.remove('image-loading');
            image.src = cache[source];
            enableImageLightbox(image);
            return Promise.resolve();
        }}
        return callBridge('resolve_image', documentPath, source).then(function(response) {{
            if (generation !== renderGeneration || !image.isConnected) return;
            if (!response || !response.ok || !response.data_url) {{
                markImageBroken(image, (response && response.error) || 'Image could not be resolved');
                return;
            }}
            cacheImageData(cache, source, response.data_url);
            image.classList.remove('image-loading');
            image.src = response.data_url;
            enableImageLightbox(image);
        }}).catch(function(error) {{
            if (generation === renderGeneration && image.isConnected) markImageBroken(image, error.message);
        }});
    }});
    return Promise.all(jobs);
}}

var IMAGE_CACHE_MAX_ENTRIES = 24;
var IMAGE_CACHE_MAX_ENTRY_CHARS = 8 * 1024 * 1024;

function cacheImageData(cache, source, dataUrl) {{
    if (!cache || !source || !dataUrl) return;
    if (dataUrl.length > IMAGE_CACHE_MAX_ENTRY_CHARS) return;
    var sources = Object.keys(cache);
    if (sources.length >= IMAGE_CACHE_MAX_ENTRIES) delete cache[sources[0]];
    cache[source] = dataUrl;
}}

function headingSlug(text) {{
    var slug = (text || '').normalize('NFKD').toLowerCase().trim()
        .replace(/[^\\p{{L}}\\p{{N}}]+/gu, '-').replace(/^-+|-+$/g, '');
    return slug || 'section';
}}

function scrollToHeading(target, behavior) {{
    var content = document.getElementById('content');
    if (!target || !content) return;
    var delta = target.getBoundingClientRect().top - content.getBoundingClientRect().top;
    content.scrollTo({{ top: content.scrollTop + delta - 16, behavior: behavior || 'smooth' }});
}}

function rebuildToc() {{
    var headings = Array.from(document.querySelectorAll('#content h1, #content h2, #content h3, #content h4, #content h5, #content h6'));
    var counts = Object.create(null);
    var list = document.getElementById('toc-list');
    list.innerHTML = '';
    tocButtons = new Map();
    var fragment = document.createDocumentFragment();
    headings.forEach(function(heading) {{
        var slug = headingSlug(heading.textContent);
        counts[slug] = (counts[slug] || 0) + 1;
        heading.id = 'previewmd-heading-' + slug + (counts[slug] > 1 ? '-' + counts[slug] : '');
        var item = document.createElement('li');
        var button = document.createElement('button');
        button.type = 'button';
        button.textContent = heading.textContent.trim() || 'Untitled heading';
        button.style.paddingLeft = (8 + (parseInt(heading.tagName.slice(1), 10) - 1) * 10) + 'px';
        button.dataset.headingId = heading.id;
        button.onclick = function() {{
            scrollToHeading(document.getElementById(button.dataset.headingId));
        }};
        item.appendChild(button);
        fragment.appendChild(item);
        tocButtons.set(heading.id, button);
    }});
    list.appendChild(fragment);
    document.getElementById('toc-empty').style.display = headings.length ? 'none' : 'block';
    activeTocId = null;
    updateActiveHeading();
}}

function updateActiveHeading() {{
    if (!document.body.classList.contains('toc-open')) return;
    var content = document.getElementById('content');
    var headings = Array.from(content.querySelectorAll('h1, h2, h3, h4, h5, h6'));
    var contentTop = content.getBoundingClientRect().top;
    var current = headings.length ? headings[0] : null;
    for (var i = 0; i < headings.length; i++) {{
        if (headings[i].getBoundingClientRect().top - contentTop <= 48) current = headings[i];
        else break;
    }}
    setActiveTocHeading(current ? current.id : null);
}}

function setActiveTocHeading(headingId) {{
    if (headingId === activeTocId) return;
    var previous = tocButtons.get(activeTocId);
    if (previous) {{
        previous.classList.remove('active');
        previous.removeAttribute('aria-current');
    }}
    activeTocId = headingId;
    var current = tocButtons.get(activeTocId);
    if (current) {{
        current.classList.add('active');
        current.setAttribute('aria-current', 'location');
    }}
}}

// Scroll events fire in bursts; collapse them into one measurement per frame.
var activeHeadingFrame = null;
function scheduleActiveHeadingUpdate() {{
    if (activeHeadingFrame !== null) return;
    activeHeadingFrame = requestAnimationFrame(function() {{
        activeHeadingFrame = null;
        updateActiveHeading();
    }});
}}

// Large documents are mounted in timed batches so the first screen appears
// immediately and the main thread keeps yielding between batches.
var PROGRESSIVE_RENDER_BUDGET_MS = 14;
var PROGRESSIVE_MIN_CHUNKS = 30;

// setTimeout is clamped to roughly a second while the window is occluded, which
// would stall a large render for minutes in the background. MessageChannel tasks
// stay prompt, so batches are scheduled on a fresh channel per render.
function createRenderScheduler() {{
    if (typeof MessageChannel !== 'function') {{
        return function(callback) {{ setTimeout(callback, 0); }};
    }}
    var channel = new MessageChannel();
    var pending = null;
    channel.port1.onmessage = function() {{
        var callback = pending;
        pending = null;
        if (callback) callback();
    }};
    return function(callback) {{
        pending = callback;
        channel.port2.postMessage(null);
    }};
}}

function renderContent(text, documentPath, preserveScroll, tab, force) {{
    var contentElement = document.getElementById('content');
    // Re-rendering identical content (view switches, export, conflict refresh)
    // costs seconds on a large document and cannot change the result.
    if (!force && lastRenderState && lastRenderState.content === text &&
        lastRenderState.tabId === (tab ? tab.id : null) && contentElement.childElementCount > 0) {{
        return lastRenderState.promise;
    }}
    var previousScroll = preserveScroll === false ? 0 : contentElement.scrollTop;
    var generation = ++renderGeneration;
    var scheduleStep = createRenderScheduler();
    resetMathRendering();

    var split = splitMarkdownChunks(protectEscapedDollars(text));
    var prefix = split.definitions.length ? split.definitions.join('\\n') + '\\n\\n' : '';
    var chunks = split.chunks;
    var mounted = 0;
    var imageJobs = [];
    var firstScroll = 0;
    var finished = false;
    var resolveRender = null;
    var renderPromise = new Promise(function(resolve) {{ resolveRender = resolve; }});

    contentElement.replaceChildren();

    function mountBatch(budgetStart, minChunks) {{
        var batchFragment = document.createDocumentFragment();
        var batchCandidates = [];
        while (mounted < chunks.length) {{
            var template = document.createElement('template');
            template.innerHTML = marked.parse(prefix + chunks[mounted]);
            sanitizeFragment(template.content);
            batchCandidates = batchCandidates.concat(prepareMathCandidates(template.content));
            imageJobs.push(prepareImages(documentPath || null, generation, tab, template.content));
            batchFragment.appendChild(template.content);
            mounted += 1;
            if (mounted >= minChunks && (performance.now() - budgetStart) >= PROGRESSIVE_RENDER_BUDGET_MS) break;
        }}
        // One attachment per batch keeps style/layout work proportional to the
        // number of batches instead of the number of chunks.
        contentElement.appendChild(batchFragment);
        scheduleMathRendering(batchCandidates, contentElement);
    }}

    function finishRender() {{
        if (finished) return;
        finished = true;
        if (generation !== renderGeneration) {{
            resolveRender();
            return;
        }}
        rebuildToc();
        // Only re-apply the restored scroll while the user has not scrolled away.
        if (contentElement.scrollTop === firstScroll) {{
            contentElement.scrollTop = Math.min(previousScroll, contentElement.scrollHeight);
        }}
        Promise.all(imageJobs).then(function() {{ resolveRender(); }}, function() {{ resolveRender(); }});
    }}

    if (chunks.length > 0) {{
        mountBatch(performance.now(), PROGRESSIVE_MIN_CHUNKS);
        firstScroll = contentElement.scrollTop = Math.min(previousScroll, contentElement.scrollHeight);
    }}

    if (mounted < chunks.length) {{
        var step = function() {{
            if (generation !== renderGeneration) {{
                finished = true;
                resolveRender();
                return;
            }}
            mountBatch(performance.now(), 1);
            if (mounted < chunks.length) scheduleStep(step);
            else finishRender();
        }};
        scheduleStep(step);
    }} else {{
        finishRender();
    }}

    lastRenderState = {{
        generation: generation,
        tabId: tab ? tab.id : null,
        content: text,
        promise: renderPromise
    }};
    return renderPromise;
}}

function setEditorContent(content, preserveSelection) {{
    var editor = document.getElementById('editor');
    var selectionStart = editor.selectionStart;
    var selectionEnd = editor.selectionEnd;
    var selectionDirection = editor.selectionDirection;
    var scrollTop = editor.scrollTop;
    editor.value = content;
    if (preserveSelection) {{
        var contentLength = content.length;
        editor.setSelectionRange(
            Math.min(selectionStart, contentLength),
            Math.min(selectionEnd, contentLength),
            selectionDirection
        );
        editor.scrollTop = scrollTop;
    }}
}}

// ── Per-tab undo and redo ──
var UNDO_SNAPSHOT_DELAY = 500;
var UNDO_STACK_LIMIT = 200;
var UNDO_STACK_MAX_CHARS = 2 * 1024 * 1024;

function resetUndoHistory(tab, content) {{
    clearTimeout(tab.undoTimer);
    tab.undoTimer = null;
    tab.undoStack = [];
    tab.redoStack = [];
    tab.undoBaseline = content;
}}

function trimUndoStack(tab) {{
    while (tab.undoStack.length > UNDO_STACK_LIMIT) tab.undoStack.shift();
    var total = 0;
    for (var i = 0; i < tab.undoStack.length; i++) total += tab.undoStack[i].content.length;
    while (tab.undoStack.length > 1 && total > UNDO_STACK_MAX_CHARS) {{
        total -= tab.undoStack.shift().content.length;
    }}
}}

function currentEditorSelection() {{
    var editor = document.getElementById('editor');
    if (!editor) return {{ start: 0, end: 0 }};
    return {{ start: editor.selectionStart, end: editor.selectionEnd }};
}}

function commitUndoSnapshot(tab) {{
    if (!tab) return;
    clearTimeout(tab.undoTimer);
    tab.undoTimer = null;
    if (tabs.indexOf(tab) < 0 || tab.content === tab.undoBaseline) return;
    tab.undoStack.push({{
        content: tab.undoBaseline,
        start: currentEditorSelection().start,
        end: currentEditorSelection().end
    }});
    trimUndoStack(tab);
    tab.redoStack = [];
    tab.undoBaseline = tab.content;
}}

function scheduleUndoSnapshot(tab) {{
    clearTimeout(tab.undoTimer);
    tab.undoTimer = setTimeout(function() {{
        tab.undoTimer = null;
        commitUndoSnapshot(tab);
    }}, UNDO_SNAPSHOT_DELAY);
}}

function setEditorSelection(selection) {{
    if (!selection) return;
    var editor = document.getElementById('editor');
    var length = editor.value.length;
    var start = Math.min(Math.max(0, selection.start || 0), length);
    var end = Math.min(Math.max(start, selection.end || 0), length);
    editor.setSelectionRange(start, end);
}}

function applyEditorSnapshot(tab, content, selection) {{
    clearTimeout(liveRenderTimer);
    tab.content = content;
    tab.dirty = tab.content !== tab.savedContent;
    tab.externallyChanged = false;
    setEditorContent(content, false);
    setEditorSelection(selection);
    tab.undoBaseline = content;
    if (hasPreview()) renderContent(tab.content, tab.path, true, tab);
    refreshTab(tab);
    syncWindowTitle();
    if (isSearchOpen() && searchQuery) refreshEditorSearch();
    scheduleAutosave(tab);
}}

function undoEditor() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return false;
    var tab = tabs[activeIdx];
    commitUndoSnapshot(tab);
    if (!tab.undoStack.length) return false;
    var entry = tab.undoStack.pop();
    tab.redoStack.push({{
        content: tab.content,
        start: currentEditorSelection().start,
        end: currentEditorSelection().end
    }});
    applyEditorSnapshot(tab, entry.content, entry);
    return true;
}}

function redoEditor() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return false;
    var tab = tabs[activeIdx];
    if (!tab.redoStack.length) return false;
    var entry = tab.redoStack.pop();
    tab.undoStack.push({{
        content: tab.content,
        start: currentEditorSelection().start,
        end: currentEditorSelection().end
    }});
    trimUndoStack(tab);
    applyEditorSnapshot(tab, entry.content, entry);
    return true;
}}

// ── Render tab bar ──
function tabStateClass(tabState) {{
    if (tabState.conflict) return ' conflict';
    if (tabState.dirty) return ' dirty';
    if (tabState.externallyChanged) return ' externally-changed';
    return ' saved';
}}

function tabStateLabel(tabState) {{
    if (tabState.conflict) return ', save conflict';
    if (tabState.dirty) return ', unsaved changes';
    if (tabState.externallyChanged) return ', changed on disk';
    return ', saved';
}}

function tabElement(tabState) {{
    return document.querySelector('#tabs [data-tab-id="' + tabState.id + '"]');
}}

function refreshTab(tabState) {{
    var element = tabElement(tabState);
    if (!element) return;
    var stateLabel = tabStateLabel(tabState);
    element.className = 'tab' + (tabs.indexOf(tabState) === activeIdx ? ' active' : '') + tabStateClass(tabState);
    element.setAttribute('aria-label', 'Open ' + tabState.name + stateLabel);
    element.title = tabState.path ? (tabState.name + stateLabel + '\\n' + tabState.path) : (tabState.name + stateLabel);
    var name = element.querySelector('.tab-name');
    if (name && name.textContent !== tabState.name) name.textContent = tabState.name;
    syncNativeDirtyState();
}}

function renderTabs() {{
    var container = document.getElementById('tabs');
    container.innerHTML = '';

    for (var i = 0; i < tabs.length; i++) {{
        (function(idx) {{
            var tabState = tabs[idx];
            var tab = document.createElement('div');
            tab.dataset.tabId = String(tabState.id);
            tab.setAttribute('role', 'button');
            tab.setAttribute('tabindex', '0');
            tab.onclick = function(e) {{
                if (e.target.classList.contains('tab-close')) return;
                switchTab(idx);
            }};
            tab.onkeydown = function(e) {{
                if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); switchTab(idx); }}
            }};

            var dot = document.createElement('span');
            dot.className = 'tab-dot';

            var name = document.createElement('span');
            name.className = 'tab-name';
            name.textContent = tabState.name;

            var close = document.createElement('button');
            close.className = 'tab-close';
            close.type = 'button';
            close.setAttribute('aria-label', 'Close ' + tabState.name);
            close.innerHTML = '&times;';
            close.onclick = function(e) {{
                e.stopPropagation();
                closeTab(idx);
            }};
            close.onkeydown = function(e) {{ e.stopPropagation(); }};

            tab.appendChild(dot);
            tab.appendChild(name);
            tab.appendChild(close);
            container.appendChild(tab);
            refreshTab(tabState);
        }})(i);
    }}
    syncNativeDirtyState();
}}

// ── Render active tab content ──
function renderActiveTab() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    document.body.classList.add('has-tabs');
    syncConflictBanner();
    if (hasPreview()) {{
        renderContent(tab.content, tab.path, false, tab);
        document.getElementById('content').scrollTop = tab.previewScroll || 0;
    }}
    if (hasEditor()) {{
        var editor = document.getElementById('editor');
        setEditorContent(tab.content, false);
        editor.scrollTop = tab.editorScroll || 0;
        editorSessionGeneration += 1;
    }}
}}

// ── Update window title ──
var lastSentTitle = null;

function syncWindowTitle() {{
    var title;
    if (activeIdx >= 0 && activeIdx < tabs.length) {{
        var tab = tabs[activeIdx];
        var marker = tab.conflict ? '⚠ ' : (tab.dirty ? '● ' : '');
        title = marker + tab.name + ' - PreviewMD';
    }} else {{
        title = 'PreviewMD';
    }}
    document.title = title;
    if (!window.pywebview || !window.pywebview.api) return;
    if (title === lastSentTitle) return;
    lastSentTitle = title;
    callBridge('set_title', title).catch(function() {{
        lastSentTitle = null;
    }});
}}

function markTabConflict(tab, diskContent, diskHash, message) {{
    clearTimeout(tab.saveTimer);
    tab.conflictVersion += 1;
    tab.conflict = true;
    tab.dirty = true;
    tab.externallyChanged = true;
    tab.externalContent = diskContent;
    tab.conflictBannerDismissed = false;
    if (diskHash) tab.diskHash = diskHash;
    refreshTab(tab);
    syncWindowTitle();
    if (tabs[activeIdx] === tab) {{
        flashActiveDot();
        showConflictBanner();
    }}
    showStatus(message || 'Conflict: this file changed on disk. Autosave is paused.');
}}

function showConflictBanner() {{
    var banner = document.getElementById('conflict-banner');
    if (banner) banner.classList.add('visible');
}}

function hideConflictBanner() {{
    var banner = document.getElementById('conflict-banner');
    if (banner) banner.classList.remove('visible');
}}

function syncConflictBanner() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) {{ hideConflictBanner(); return; }}
    var tab = tabs[activeIdx];
    if (tab.conflict && !tab.conflictBannerDismissed) showConflictBanner();
    else hideConflictBanner();
}}

function keepLocalEdits() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    tabs[activeIdx].conflictBannerDismissed = true;
    hideConflictBanner();
    showStatus('Keeping your edits. Autosave stays paused; Cmd+S overwrites the disk version.');
}}

// Replace a tab's content with a freshly read disk snapshot.
function applyDiskSnapshot(tab, content, diskHash) {{
    if (tabs.indexOf(tab) < 0) return;
    var isActive = tabs[activeIdx] === tab;
    if (isActive && document.getElementById('search-bar').style.display === 'block') closeSearch();
    var changed = tab.content !== content;
    tab.content = content;
    tab.savedContent = content;
    tab.dirty = false;
    tab.conflict = false;
    tab.conflictVersion += 1;
    tab.diskHash = diskHash || tab.diskHash;
    tab.externallyChanged = changed;
    tab.externalContent = null;
    tab.conflictBannerDismissed = false;
    tab.imageCache = {{}};
    resetUndoHistory(tab, content);
    if (isActive) {{
        if (hasPreview()) renderContent(content, tab.path, true, tab);
        if (hasEditor()) {{
            editorSessionGeneration += 1;
            setEditorContent(content, true);
        }}
        if (changed) flashActiveDot();
        syncConflictBanner();
    }}
    refreshTab(tab);
    syncWindowTitle();
}}

// ── Add or reopen a tab ──
function addTab(name, path, content, diskHash) {{
    var existing = findTabByPath(path);
    if (existing >= 0) {{
        var existingTab = tabs[existing];
        if (existingTab.dirty) {{
            markTabConflict(existingTab, content, diskHash, 'Conflict: reopened file has unsaved edits; autosave is paused.');
        }} else {{
            existingTab.content = content;
            existingTab.savedContent = content;
            existingTab.diskHash = diskHash || existingTab.diskHash;
            existingTab.conflict = false;
            existingTab.externallyChanged = true;
            existingTab.externalContent = null;
            existingTab.imageCache = {{}};
            resetUndoHistory(existingTab, content);
            if (existing === activeIdx) {{
                if (document.getElementById('search-bar').style.display === 'block') closeSearch();
                if (hasPreview()) renderContent(content, path, true, existingTab);
                if (hasEditor()) {{
                    editorSessionGeneration += 1;
                    setEditorContent(content, true);
                }}
            }}
        }}
        switchTab(existing);
        renderTabs();
        syncWindowTitle();
        syncSession();
        return;
    }}
    if (document.getElementById('search-bar').style.display === 'block') closeSearch();
    tabs.push({{
        id: nextTabId++, name: name, path: path, content: content, savedContent: content,
        diskHash: diskHash || null,
        dirty: false, conflict: false, externallyChanged: false, externalContent: null,
        conflictVersion: 0, conflictBannerDismissed: false, saveTimer: null, savePromise: null, editorScroll: 0, previewScroll: 0,
        imageCache: {{}}, undoStack: [], redoStack: [], undoTimer: null, undoBaseline: content
    }});
    activeIdx = tabs.length - 1;

    renderTabs();
    renderActiveTab();
    syncWindowTitle();
    syncSession();
}}

// ── Close a tab ──
async function closeTab(index) {{
    if (index < 0 || index >= tabs.length) return;
    var tab = tabs[index];
    captureActiveScrolls();
    clearTimeout(tab.saveTimer);
    if (tab.dirty) {{
        if (!tab.path) {{
            if (!window.confirm('Discard unsaved changes in "' + tab.name + '"?')) return;
        }} else {{
            await saveTab(tab, false);
            if (tab.dirty && !window.confirm('Changes to "' + tab.name + '" could not be fully saved. Close and discard them?')) return;
        }}
    }}
    index = tabs.indexOf(tab);
    if (index < 0) return;
    if (index === activeIdx && document.getElementById('search-bar').style.display === 'block') closeSearch();

    var path = tab.path;
    if (path && window.pywebview) {{
        window.pywebview.api.stop_watching(path);
    }}

    tabs.splice(index, 1);

    if (tabs.length === 0) {{
        activeIdx = -1;
        document.body.classList.remove('has-tabs');
        document.getElementById('content').innerHTML = '';
        document.getElementById('editor').value = '';
        clearTimeout(liveRenderTimer);
        editorSessionGeneration += 1;
        closeSearch();
        closeExportMenu();
        closeLightbox();
        hideConflictBanner();
        document.body.classList.remove('toc-open');
        document.getElementById('tab-toc').setAttribute('aria-expanded', 'false');
        rebuildToc();
        syncWindowTitle();
        syncNativeDirtyState();
        syncSession();
        return;
    }}

    if (index < activeIdx) activeIdx -= 1;
    else if (activeIdx >= tabs.length || index === activeIdx) activeIdx = Math.min(index, tabs.length - 1);
    renderTabs();
    renderActiveTab();
    syncWindowTitle();
    syncSession();
}}

// ── Switch to a tab ──
async function switchTab(index) {{
    if (index < 0 || index >= tabs.length || index === activeIdx) return;
    var previous = activeIdx >= 0 ? tabs[activeIdx] : null;
    var target = tabs[index];
    closeSearch();
    captureActiveScrolls();
    commitUndoSnapshot(previous);
    if (previous && previous.path && previous.dirty && !previous.conflict) await saveTab(previous, false);
    index = tabs.indexOf(target);
    if (index < 0) return;
    activeIdx = index;
    renderTabs();
    renderActiveTab();
    syncWindowTitle();
    syncSession();
}}

// ── Flash change indicator on active tab ──
function flashActiveDot() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var element = tabElement(tabs[activeIdx]);
    var dot = element ? element.querySelector('.tab-dot') : null;
    if (!dot) return;
    dot.classList.add('changed');
    setTimeout(function() {{ dot.classList.remove('changed'); }}, 800);
}}

// ── View modes, dirty state, and saving ──
var viewMode = 'preview';
var editorSessionGeneration = 0;

function hasEditor() {{
    return viewMode === 'edit' || viewMode === 'split';
}}

function hasPreview() {{
    return viewMode === 'preview' || viewMode === 'split';
}}

function isSplitView() {{
    return viewMode === 'split';
}}

function syncViewControls() {{
    ['preview', 'edit', 'split'].forEach(function(mode) {{
        document.getElementById('mode-' + mode).setAttribute('aria-pressed', String(viewMode === mode));
    }});
    document.getElementById('tab-image').classList.toggle('editing-visible', hasEditor());
    document.getElementById('tab-toc').disabled = !hasPreview();
}}

function mapScrollRatio(sourceRange, sourcePos, target) {{
    var targetRange = target.scrollHeight - target.clientHeight;
    if (sourceRange > 0 && targetRange > 0) {{
        target.scrollTop = (sourcePos / sourceRange) * targetRange;
    }}
}}

function setViewMode(nextMode) {{
    if (tabs.length === 0 || ['preview', 'edit', 'split'].indexOf(nextMode) < 0 || nextMode === viewMode) return;
    var tab = activeIdx >= 0 ? tabs[activeIdx] : null;
    var previouslyHadEditor = hasEditor();
    var previouslyHadPreview = hasPreview();

    var editor = document.getElementById('editor');
    var preview = document.getElementById('content');
    var sourcePreviewPos = previouslyHadPreview ? preview.scrollTop : 0;
    var sourcePreviewRange = previouslyHadPreview ? (preview.scrollHeight - preview.clientHeight) : 0;
    var sourceEditorPos = previouslyHadEditor ? editor.scrollTop : 0;
    var sourceEditorRange = previouslyHadEditor ? (editor.scrollHeight - editor.clientHeight) : 0;

    if (document.getElementById('search-bar').style.display === 'block') closeSearch();
    if (nextMode === 'edit') {{
        document.body.classList.remove('toc-open');
        document.getElementById('tab-toc').setAttribute('aria-expanded', 'false');
    }}
    if (previouslyHadEditor && nextMode === 'preview') {{
        editorSessionGeneration += 1;
        if (tab && tab.path && tab.dirty && !tab.conflict) saveTab(tab, false);
    }} else if (!previouslyHadEditor && nextMode !== 'preview') {{
        editorSessionGeneration += 1;
    }}

    clearTimeout(liveRenderTimer);
    viewMode = nextMode;
    document.body.classList.toggle('edit-mode', viewMode === 'edit');
    document.body.classList.toggle('split-mode', viewMode === 'split');
    syncViewControls();

    if (!tab) return;
    if (hasEditor() && !previouslyHadEditor) {{
        setEditorContent(tab.content, false);
        var sourceRange = (nextMode === 'split') ? (preview.scrollHeight - preview.clientHeight) : sourcePreviewRange;
        var sourcePos = (nextMode === 'split') ? preview.scrollTop : sourcePreviewPos;
        mapScrollRatio(sourceRange, sourcePos, editor);
    }}
    if (hasPreview() && !previouslyHadPreview) {{
        renderContent(tab.content, tab.path, true, tab);
        var sourceRange = (nextMode === 'split') ? (editor.scrollHeight - editor.clientHeight) : sourceEditorRange;
        var sourcePos = (nextMode === 'split') ? editor.scrollTop : sourceEditorPos;
        mapScrollRatio(sourceRange, sourcePos, preview);
    }}
    if (hasEditor()) {{
        editor.focus({{ preventScroll: true }});
        if (!tab.path && !previouslyHadEditor) showStatus('New document: press Cmd/Ctrl + S to choose where to save it.');
    }}
    captureActiveScrolls();
    syncSession();
}}

function scheduleAutosave(tab) {{
    clearTimeout(tab.saveTimer);
    if (!tab.path || !tab.dirty || tab.conflict) return;
    tab.saveTimer = setTimeout(function() {{ saveTab(tab, false); }}, 1000);
}}

async function saveTab(tab, explicitSave) {{
    if (!tab || tabs.indexOf(tab) < 0) return false;
    clearTimeout(tab.saveTimer);
    commitUndoSnapshot(tab);
    if (!tab.path) {{
        if (explicitSave) showStatus('Use Save As to give this new document a location.');
        return false;
    }}
    if (tab.conflict && !explicitSave) return false;
    if (tab.savePromise) {{
        await tab.savePromise;
        if (!tab.dirty) return true;
    }}
    var snapshot = tab.content;
    var force = false;
    if (tab.conflict) {{
        if (!explicitSave || !window.confirm('This file changed on disk. Overwrite the external version with your edits?')) return false;
        force = true;
    }}    tab.savePromise = (async function() {{
        try {{
            var attemptVersion = tab.conflictVersion;
            var response = await callBridge('save_file', tab.path, snapshot, tab.diskHash, force);
            if (response && response.conflict) {{
                markTabConflict(tab, tab.externalContent, response.disk_hash, 'Conflict: disk content changed. Autosave is paused.');
                if (!explicitSave || force || !window.confirm('This file changed on disk. Overwrite the external version with your edits?')) return false;
                force = true;
                attemptVersion = tab.conflictVersion;
                response = await callBridge('save_file', tab.path, snapshot, tab.diskHash, true);
            }}
            if (!response || !response.ok) throw new Error((response && response.error) || 'Save failed.');
            if (tab.conflictVersion !== attemptVersion) {{
                tab.conflict = true;
                tab.dirty = true;
                showStatus('Conflict: the file changed while saving. Autosave is paused.');
                return false;
            }}
            tab.diskHash = response.hash;
            tab.savedContent = snapshot;
            tab.dirty = tab.content !== snapshot;
            tab.conflict = false;
            tab.externallyChanged = false;
            tab.externalContent = null;
            showStatus('Saved.');
            if (tabs.indexOf(tab) >= 0) {{ refreshTab(tab); syncWindowTitle(); }}
            if (tab.dirty) scheduleAutosave(tab);
            return true;
        }} catch (error) {{
            showStatus(error.message || 'Save failed.');
            return false;
        }} finally {{
            tab.savePromise = null;
            syncNativeDirtyState();
        }}
    }})();
    syncNativeDirtyState();
    return await tab.savePromise;
}}

function saveFile() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return Promise.resolve(false);
    var tab = tabs[activeIdx];
    if (!tab.path) return saveFileAs();
    return saveTab(tab, true);
}}

function newUntitledTab() {{
    untitledCounter += 1;
    var name = untitledCounter === 1 ? 'Untitled.md' : 'Untitled-' + untitledCounter + '.md';
    addTab(name, null, '', null);
    if (!hasEditor()) setViewMode('edit');
    else document.getElementById('editor').focus({{ preventScroll: true }});
}}

async function reloadActiveTab() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    if (!tab.path) {{
        tab.imageCache = {{}};
        if (hasPreview()) renderContent(tab.content, tab.path, true, tab, true);
        showStatus('Reloaded local images for this document.');
        return;
    }}
    if (tab.dirty && !window.confirm('This tab has unsaved edits. Discard them and load the version on disk?')) return;
    var response;
    try {{
        response = await callBridge('reload_file', tab.path);
    }} catch (error) {{
        showStatus((error && error.message) || 'Reload failed.');
        return;
    }}
    if (!response || !response.ok) {{
        showStatus((response && response.error) || 'Reload failed.');
        return;
    }}
    if (tabs.indexOf(tab) < 0) return;
    applyDiskSnapshot(tab, response.content, response.hash);
    showStatus('Reloaded from disk.');
}}

async function saveFileAs() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return false;
    var tab = tabs[activeIdx];
    if (tab.savePromise) await tab.savePromise;
    var snapshot = tab.content;
    var previousPath = tab.path;
    var suggested = tab.name || 'Untitled.md';
    var response;
    try {{
        response = await callBridge('save_file_as', suggested, snapshot, previousPath || null);
    }} catch (error) {{
        showStatus(error.message || 'Save failed.');
        return false;
    }}
    if (!response || !response.ok) {{
        if (!response || !response.cancelled) showStatus((response && response.error) || 'Save failed.');
        return false;
    }}
    if (tabs.indexOf(tab) < 0) return false;
    if (previousPath && previousPath !== response.path && window.pywebview) {{
        callBridge('stop_watching', previousPath).catch(function() {{}});
    }}
    tab.path = response.path;
    tab.name = response.name;
    tab.diskHash = response.hash;
    tab.savedContent = snapshot;
    tab.dirty = tab.content !== snapshot;
    tab.conflict = false;
    tab.externallyChanged = false;
    tab.externalContent = null;
    renderTabs();
    syncWindowTitle();
    syncSession();
    showStatus('Saved to ' + (response.display_path || response.name) + '.');
    if (tab.dirty) scheduleAutosave(tab);
    return true;
}}

function captureActiveScrolls() {{
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    if (hasEditor()) tab.editorScroll = document.getElementById('editor').scrollTop;
    if (hasPreview()) tab.previewScroll = document.getElementById('content').scrollTop;
}}

document.getElementById('editor').addEventListener('input', function() {{
    if (!hasEditor() || activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    tab.content = this.value;
    tab.dirty = tab.content !== tab.savedContent;
    tab.externallyChanged = false;
    refreshTab(tab);
    syncWindowTitle();
    if (isSearchOpen() && searchQuery) refreshEditorSearch();
    clearTimeout(liveRenderTimer);
    if (isSplitView()) {{
        liveRenderTimer = setTimeout(function() {{
            if (isSplitView() && activeIdx >= 0 && tabs[activeIdx] === tab) renderContent(tab.content, tab.path, true, tab);
        }}, liveRenderDelay(tab.content));
    }}
    scheduleUndoSnapshot(tab);
    scheduleAutosave(tab);
}});

var scrollSyncSource = null;
function syncProportionalScroll(source, target) {{
    if (!isSplitView()) return;
    var sourceRange = source.scrollHeight - source.clientHeight;
    var targetRange = target.scrollHeight - target.clientHeight;
    if (sourceRange <= 0 || targetRange <= 0) return;
    scrollSyncSource = target;
    target.scrollTop = (source.scrollTop / sourceRange) * targetRange;
}}

document.getElementById('editor').addEventListener('scroll', function() {{
    if (activeIdx >= 0) tabs[activeIdx].editorScroll = this.scrollTop;
    syncEditorHighlightScroll();
    if (this === scrollSyncSource) {{ scrollSyncSource = null; return; }}
    syncProportionalScroll(this, document.getElementById('content'));
}});
document.getElementById('content').addEventListener('scroll', function() {{
    if (activeIdx >= 0) tabs[activeIdx].previewScroll = this.scrollTop;
    var suppressed = (this === scrollSyncSource);
    if (suppressed) scrollSyncSource = null;
    scheduleActiveHeadingUpdate();
    if (!suppressed) syncProportionalScroll(this, document.getElementById('editor'));
}});

document.getElementById('content').addEventListener('click', function(event) {{
    var link = event.target.closest && event.target.closest('a');
    if (!link || !this.contains(link)) return;
    var destination = (link.getAttribute('href') || '').trim();
    if (destination.charAt(0) === '#') {{
        event.preventDefault();
        var anchorId;
        try {{ anchorId = decodeURIComponent(destination.slice(1)); }} catch (error) {{ anchorId = destination.slice(1); }}
        var target = anchorId ? document.getElementById(anchorId) : null;
        if (target) scrollToHeading(target);
        return;
    }}
    event.preventDefault();
    if (/^(https?:|mailto:)/i.test(destination)) {{
        callBridge('open_external_url', destination).then(function(response) {{
            if (!response || !response.ok) showStatus((response && response.error) || 'Could not open external link.');
        }}).catch(function() {{ showStatus('Could not open external link.'); }});
    }} else {{
        showStatus('Navigation was blocked to keep your document open.');
    }}
}});

window.addEventListener('beforeunload', function(event) {{
    if (!hasUnsavedWork()) return;
    event.preventDefault();
    event.returnValue = '';
    return '';
}});

function toggleToc() {{
    if (!hasPreview()) return;
    var open = !document.body.classList.contains('toc-open');
    document.body.classList.toggle('toc-open', open);
    document.getElementById('tab-toc').setAttribute('aria-expanded', String(open));
    if (open) updateActiveHeading();
}}

function closeExportMenu() {{
    document.getElementById('export-menu').classList.remove('open');
    document.getElementById('tab-export').setAttribute('aria-expanded', 'false');
}}

function toggleExportMenu(event) {{
    if (event) event.stopPropagation();
    var menu = document.getElementById('export-menu');
    var opening = !menu.classList.contains('open');
    closeExportMenu();
    if (opening) {{
        var button = document.getElementById('tab-export');
        var rect = button.getBoundingClientRect();
        menu.style.top = (rect.bottom + 4) + 'px';
        menu.style.left = Math.max(6, rect.right - 190) + 'px';
        menu.classList.add('open');
        button.setAttribute('aria-expanded', 'true');
        menu.querySelector('button').focus();
    }}
}}
document.addEventListener('click', function(event) {{
    if (!document.getElementById('export-wrap').contains(event.target)) closeExportMenu();
}});

async function awaitStableExportRender(tab) {{
    for (var attempt = 0; attempt < 8; attempt++) {{
        if (tabs.indexOf(tab) < 0 || tabs[activeIdx] !== tab) return false;
        var state = lastRenderState;
        if (!state || state.tabId !== tab.id || state.content !== tab.content) {{
            renderContent(tab.content, tab.path, true, tab);
            state = lastRenderState;
        }}
        try {{ await state.promise; }} catch (error) {{ return false; }}
        await new Promise(function(resolve) {{ setTimeout(resolve, 0); }});
        var isStable = state === lastRenderState && state.generation === renderGeneration &&
            state.tabId === tab.id && state.content === tab.content && tabs[activeIdx] === tab;
        if (isStable) return true;
    }}
    return false;
}}

async function exportHtml() {{
    closeExportMenu();
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    if (!await awaitStableExportRender(tab)) {{
        showStatus('Preview kept changing; pause editing and try export again.');
        return;
    }}
    forceRenderAllMath();
    var clone = document.getElementById('content').cloneNode(true);
    clone.querySelectorAll('mark.search-highlight').forEach(function(mark) {{ mark.replaceWith(document.createTextNode(mark.textContent)); }});
    clone.querySelectorAll('img').forEach(function(image) {{
        image.classList.remove('image-loading');
        image.removeAttribute('loading');
        image.removeAttribute('decoding');
        delete image.dataset.lightboxReady;
    }});
    var stylesheet = Array.from(document.querySelectorAll('head style')).map(function(style) {{ return style.textContent; }}).join('\\n');
    stylesheet += '\\nhtml,body{{height:auto;overflow:visible}}body{{padding:24px}}.markdown-body{{max-width:900px;margin:0 auto}}';
    try {{
        var response = await callBridge('export_html', tab.name, clone.innerHTML, stylesheet);
        if (response && response.cancelled) return;
        showStatus(response && response.ok ? 'HTML exported.' : ((response && response.error) || 'Export failed.'));
    }} catch (error) {{ showStatus(error.message || 'Export failed.'); }}
}}

async function printDocument() {{
    closeExportMenu();
    if (activeIdx < 0 || activeIdx >= tabs.length) return;
    var tab = tabs[activeIdx];
    if (!await awaitStableExportRender(tab)) {{
        showStatus('Preview kept changing; pause editing and try printing again.');
        return;
    }}
    forceRenderAllMath();
    window.print();
}}

var lightboxZoom = 1;
var lightboxNaturalWidth = 1;
var lightboxPreviousFocus = null;
function setLightboxZoom(value) {{
    lightboxZoom = Math.max(0.25, Math.min(5, value));
    document.getElementById('lightbox-image').style.width = Math.max(1, lightboxNaturalWidth * lightboxZoom) + 'px';
    document.getElementById('lightbox-reset').textContent = Math.round(lightboxZoom * 100) + '%';
}}
function openLightbox(sourceImage) {{
    lightboxPreviousFocus = document.activeElement;
    var dialog = document.getElementById('lightbox');
    var image = document.getElementById('lightbox-image');
    image.src = sourceImage.currentSrc || sourceImage.src;
    image.alt = sourceImage.alt || '';
    lightboxNaturalWidth = sourceImage.naturalWidth || sourceImage.clientWidth || 1;
    document.getElementById('lightbox-caption').textContent = sourceImage.alt || '';
    dialog.classList.add('open');
    dialog.setAttribute('aria-hidden', 'false');
    setLightboxZoom(1);
    document.getElementById('lightbox-close').focus();
}}
function closeLightbox() {{
    var dialog = document.getElementById('lightbox');
    if (!dialog.classList.contains('open')) return;
    dialog.classList.remove('open');
    dialog.setAttribute('aria-hidden', 'true');
    document.getElementById('lightbox-image').removeAttribute('src');
    if (lightboxPreviousFocus && lightboxPreviousFocus.focus) lightboxPreviousFocus.focus();
}}
document.getElementById('lightbox-plus').onclick = function() {{ setLightboxZoom(lightboxZoom + .25); }};
document.getElementById('lightbox-minus').onclick = function() {{ setLightboxZoom(lightboxZoom - .25); }};
document.getElementById('lightbox-reset').onclick = function() {{ setLightboxZoom(1); }};
document.getElementById('lightbox-close').onclick = closeLightbox;
document.getElementById('lightbox').addEventListener('click', function(event) {{ if (event.target === this) closeLightbox(); }});
document.getElementById('lightbox').addEventListener('wheel', function(event) {{
    event.preventDefault();
    setLightboxZoom(lightboxZoom + (event.deltaY < 0 ? .15 : -.15));
}}, {{ passive: false }});

function filenameAlt(filename) {{
    var name = (filename || 'image').split('/').pop();
    try {{ name = decodeURIComponent(name); }} catch (error) {{}}
    return name.replace(/\\.[^.]+$/, '').replace(/[_-]+/g, ' ').trim() || 'image';
}}

function escapeMarkdownAlt(value) {{
    return value.replace(/\\s+/g, ' ').trim().split('\\\\').join('\\\\\\\\').split(']').join('\\\\]');
}}

function captureImageImportContext() {{
    return {{ documentPath: activeDocumentPath(), editorSession: editorSessionGeneration, tab: tabs[activeIdx] }};
}}

function isCurrentImageImportContext(context) {{
    return hasEditor() && context && context.editorSession === editorSessionGeneration &&
        context.tab === tabs[activeIdx] && context.documentPath === activeDocumentPath();
}}

function insertImportedImage(response, preferredAlt, context) {{
    if (!response || !response.ok || !response.path) {{
        if (response && response.needs_path) {{
            showStatus('Save this document first, then insert the image again.');
            return;
        }}
        showStatus((response && response.error) || 'Image import failed.');
        return;
    }}
    if (!isCurrentImageImportContext(context)) {{
        showStatus('Image was copied to assets, but the editor changed before it could be inserted.');
        return;
    }}
    var editor = document.getElementById('editor');
    var start = editor.selectionStart;
    var end = editor.selectionEnd;
    var selected = editor.value.slice(start, end).trim();
    var alt = escapeMarkdownAlt(selected || preferredAlt || filenameAlt(response.path));
    var markdownPath = response.path.split('/').map(encodeURIComponent).join('/');
    var markdown = '![' + alt + '](' + markdownPath + ')';
    var before = editor.value.slice(0, start);
    var after = editor.value.slice(end);
    var prefix = before && !before.endsWith('\\n') ? '\\n' : '';
    var suffix = after && !after.startsWith('\\n') ? '\\n' : '';
    var insertion = prefix + markdown + suffix;
    editor.setRangeText(insertion, start, end, 'end');
    var cursor = start + prefix.length + markdown.length;
    editor.setSelectionRange(cursor, cursor);
    editor.focus();
    editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
    showStatus('Image inserted: ' + response.path);
}}

function activeDocumentPath() {{
    return activeIdx >= 0 && activeIdx < tabs.length ? tabs[activeIdx].path : null;
}}

function ensureDocumentPath() {{
    if (activeDocumentPath()) return Promise.resolve(true);
    if (activeIdx < 0 || activeIdx >= tabs.length) return Promise.resolve(false);
    showStatus('Choose where to save this document; images are stored in its assets folder.');
    return saveFileAs().then(function(saved) {{
        if (saved) return true;
        showStatus('Image was not inserted because the document has no location yet.');
        return false;
    }});
}}

function pickImage() {{
    if (!hasEditor()) return;
    ensureDocumentPath().then(function(ready) {{
        if (!ready) return;
        var documentPath = activeDocumentPath();
        if (!documentPath) return;
        var context = captureImageImportContext();
        return callBridge('pick_image', documentPath).then(function(response) {{
            if (response && response.cancelled) return;
            insertImportedImage(response, null, context);
        }});
    }}).catch(function(error) {{
        showStatus(error.message || 'Image import failed.');
    }});
}}

function importImageBlob(file) {{
    if (file.size > 20 * 1024 * 1024) {{
        showStatus('Image is larger than the 20 MB limit.');
        return;
    }}
    var supportedType = /^(image\\/png|image\\/jpeg|image\\/gif|image\\/webp)$/i.test(file.type || '') ||
        /\\.(png|jpe?g|gif|webp)$/i.test(file.name || '');
    if (!supportedType) {{
        showStatus('Only PNG, JPEG, GIF, and WebP images are supported.');
        return;
    }}
    var fileName = file.name || 'clipboard-image.png';
    ensureDocumentPath().then(function(ready) {{
        if (!ready) return;
        var documentPath = activeDocumentPath();
        if (!documentPath) return;
        var context = captureImageImportContext();
        if (file.path) {{
            return callBridge('import_image_path', documentPath, file.path).then(function(response) {{
                insertImportedImage(response, filenameAlt(file.name), context);
            }});
        }}
        return new Promise(function(resolve, reject) {{
            var reader = new FileReader();
            reader.onload = function() {{
                callBridge('import_image_data', documentPath, fileName, reader.result).then(function(response) {{
                    insertImportedImage(response, filenameAlt(file.name), context);
                    resolve();
                }}, reject);
            }};
            reader.onerror = function() {{ reject(new Error('Could not read the image.')); }};
            reader.readAsDataURL(file);
        }});
    }}).catch(function(error) {{
        showStatus((error && error.message) || 'Image import failed.');
    }});
}}

document.getElementById('editor').addEventListener('paste', function(event) {{
    var items = Array.from((event.clipboardData && event.clipboardData.items) || []);
    var imageItem = items.find(function(item) {{ return item.kind === 'file' && /^image\\//i.test(item.type); }});
    if (!imageItem) return; // Preserve ordinary text paste.
    event.preventDefault();
    var imageFile = imageItem.getAsFile();
    if (imageFile) importImageBlob(imageFile);
}});

document.getElementById('editor').addEventListener('dragover', function(event) {{
    var files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    if (files.some(function(file) {{ return /^image\\//i.test(file.type) || /\\.(png|jpe?g|gif|webp)$/i.test(file.name || ''); }})) event.preventDefault();
}});

document.getElementById('editor').addEventListener('drop', function(event) {{
    var files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    var imageFile = files.find(function(file) {{
        return /^image\\//i.test(file.type) || /\\.(png|jpe?g|gif|webp)$/i.test(file.name || '');
    }});
    if (!imageFile) return;
    event.preventDefault();
    event.stopPropagation();
    importImageBlob(imageFile);
}});

// ── Search ──
var searchMatches = [];
var currentMatchIdx = -1;
var searchQuery = '';
var searchComposing = false;
var SEARCH_DEBOUNCE_MS = 120;
var searchInputTimer = null;
// Preview search counts every hit but only hands a window of ranges to the
// engine, so documents with tens of thousands of matches stay cheap.
var SEARCH_PAINT_LIMIT = 800;
var SEARCH_RANGE_LIMIT = 20000;
var searchRanges = [];

function supportsCustomHighlight() {{
    return typeof CSS !== 'undefined' && !!CSS.highlights && typeof Highlight === 'function';
}}

function escapeRegex(s) {{
    return s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
}}

function copyEditorTextStyles(source, target) {{
    var computed = getComputedStyle(source);
    var mirroredProperties = [
        'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'lineHeight',
        'letterSpacing', 'wordSpacing', 'textIndent', 'textTransform', 'textAlign',
        'whiteSpace', 'overflowWrap', 'wordBreak', 'tabSize', 'boxSizing',
        'paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight',
        'borderTopWidth', 'borderBottomWidth', 'borderLeftWidth', 'borderRightWidth'
    ];
    mirroredProperties.forEach(function(property) {{
        target.style[property] = computed[property];
    }});
    return computed;
}}

function measureEditorOffset(editor, position) {{
    var mirror = document.createElement('div');
    copyEditorTextStyles(editor, mirror);
    mirror.style.position = 'absolute';
    mirror.style.visibility = 'hidden';
    mirror.style.left = '-9999px';
    mirror.style.top = '0';
    mirror.style.width = editor.clientWidth + 'px';
    mirror.style.height = 'auto';
    mirror.style.overflow = 'hidden';
    mirror.textContent = editor.value.slice(0, position);
    var marker = document.createElement('span');
    marker.textContent = '\\u200b';
    mirror.appendChild(marker);
    editor.parentNode.appendChild(mirror);
    var offset = marker.offsetTop;
    mirror.remove();
    return offset;
}}

function isSearchOpen() {{
    return document.getElementById('search-bar').style.display === 'block';
}}

function collectEditorMatches(text, query) {{
    var matches = [];
    var re = new RegExp(escapeRegex(query), 'gi');
    var m;
    while ((m = re.exec(text)) !== null) {{
        matches.push({{ start: m.index, end: m.index + m[0].length }});
    }}
    return matches;
}}

function syncEditorHighlightScroll() {{
    var editor = document.getElementById('editor');
    var layer = document.getElementById('editor-highlights');
    layer.scrollTop = editor.scrollTop;
}}

var editorLayerText = null;

function clearEditorHighlights() {{
    if (supportsCustomHighlight()) {{
        CSS.highlights.delete('previewmd-editor-search');
        CSS.highlights.delete('previewmd-editor-search-current');
    }}
    var marks = document.querySelectorAll('#editor-highlights mark');
    marks.forEach(function(mark) {{
        var parent = mark.parentNode;
        while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
        parent.removeChild(mark);
        parent.normalize();
    }});
}}

function editorSearchPaintWindow() {{
    var start = 0;
    var end = Math.min(searchMatches.length, SEARCH_PAINT_LIMIT);
    if (currentMatchIdx >= 0) {{
        start = Math.max(0, currentMatchIdx - Math.floor(SEARCH_PAINT_LIMIT / 2));
        end = Math.min(searchMatches.length, start + SEARCH_PAINT_LIMIT);
    }}
    return {{ start: start, end: end }};
}}

function editorLayerTextNode() {{
    var layer = document.getElementById('editor-highlights');
    var node = layer.firstChild;
    return node && node.nodeType === Node.TEXT_NODE ? node : null;
}}

function paintCurrentEditorHighlight() {{
    if (!supportsCustomHighlight()) return;
    var match = currentMatchIdx >= 0 ? searchMatches[currentMatchIdx] : null;
    var node = match ? editorLayerTextNode() : null;
    if (!match || !node) {{
        CSS.highlights.delete('previewmd-editor-search-current');
        return;
    }}
    var range = document.createRange();
    range.setStart(node, match.start);
    range.setEnd(node, match.end);
    var current = new Highlight();
    current.add(range);
    CSS.highlights.set('previewmd-editor-search-current', current);
}}

function paintEditorHighlights() {{
    var window = editorSearchPaintWindow();
    if (supportsCustomHighlight()) {{
        var node = editorLayerTextNode();
        if (!node) return;
        var painted = new Highlight();
        for (var i = window.start; i < window.end; i++) {{
            var match = searchMatches[i];
            var range = document.createRange();
            range.setStart(node, match.start);
            range.setEnd(node, match.end);
            painted.add(range);
        }}
        CSS.highlights.set('previewmd-editor-search', painted);
        paintCurrentEditorHighlight();
        return;
    }}
    // Legacy fallback: only the current hit becomes a DOM node.
    clearEditorHighlights();
    if (currentMatchIdx < 0) return;
    var current = searchMatches[currentMatchIdx];
    var textNode = editorLayerTextNode();
    if (!current || !textNode) return;
    var currentRange = document.createRange();
    currentRange.setStart(textNode, current.start);
    currentRange.setEnd(textNode, current.end);
    var mark = document.createElement('mark');
    mark.className = 'current';
    try {{
        currentRange.surroundContents(mark);
    }} catch (error) {{
        // A range crossing element boundaries cannot be surrounded; the count and
        // scrolling still work.
    }}
}}

function renderEditorHighlights() {{
    var editor = document.getElementById('editor');
    var layer = document.getElementById('editor-highlights');
    if (!hasEditor() || !isSearchOpen() || !searchQuery || searchMatches.length === 0) {{
        clearEditorHighlights();
        layer.textContent = '';
        editorLayerText = null;
        return;
    }}
    var text = editor.value;
    if (editorLayerText !== text) {{
        copyEditorTextStyles(editor, layer);
        var scrollbarWidth = editor.offsetWidth - editor.clientWidth;
        layer.style.paddingRight = (parseFloat(layer.style.paddingRight) + scrollbarWidth) + 'px';
        // Mirror the text once; matches are painted by the engine instead of one
        // <mark> per hit, which used to cost a minute on a large document.
        layer.textContent = text;
        editorLayerText = text;
    }}
    paintEditorHighlights();
    syncEditorHighlightScroll();
}}

function updateCurrentEditorHighlight() {{
    paintCurrentEditorHighlight();
}}

function refreshEditorSearch() {{
    var editor = document.getElementById('editor');
    searchMatches = collectEditorMatches(editor.value, searchQuery);
    if (currentMatchIdx >= searchMatches.length) {{
        currentMatchIdx = searchMatches.length > 0 ? searchMatches.length - 1 : -1;
    }}
    renderEditorHighlights();
    updateSearchCount();
}}

function openSearch() {{
    if (tabs.length === 0) return;

    document.getElementById('search-bar').style.display = 'block';
    var inp = document.getElementById('search-input');
    inp.value = '';
    inp.focus();
    document.getElementById('search-count').textContent = '0/0';
    clearHighlights();
    searchMatches = [];
    searchRanges = [];
    currentMatchIdx = -1;
    searchQuery = '';
    renderEditorHighlights();
}}

function closeSearch() {{
    document.getElementById('search-bar').style.display = 'none';
    clearTimeout(searchInputTimer);
    clearHighlights();
    searchMatches = [];
    searchRanges = [];
    currentMatchIdx = -1;
    searchQuery = '';
    renderEditorHighlights();
    if (hasEditor()) {{
        document.getElementById('editor').focus();
    }}
}}

function clearHighlights() {{
    if (supportsCustomHighlight()) {{
        CSS.highlights.delete('previewmd-search');
        CSS.highlights.delete('previewmd-search-current');
    }}
    var marks = document.querySelectorAll('#content mark.search-highlight');
    var touched = [];
    marks.forEach(function(m) {{
        var parent = m.parentNode;
        while (m.firstChild) parent.insertBefore(m.firstChild, m);
        parent.removeChild(m);
        if (touched.indexOf(parent) < 0) touched.push(parent);
    }});
    touched.forEach(function(parent) {{ parent.normalize(); }});
}}

function collectPreviewRanges(root, query) {{
    var ranges = [];
    if (!query) return ranges;
    var re = new RegExp(escapeRegex(query), 'gi');
    var blockedTags = new Set(['CODE', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'KBD', 'SAMP', 'MARK']);
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {{
        acceptNode: function(node) {{
            if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
            if (hasBlockedAncestor(node, blockedTags, 'katex')) return NodeFilter.FILTER_REJECT;
            re.lastIndex = 0;
            return re.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        }}
    }});

    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    nodes.forEach(function(node) {{
        if (ranges.length >= SEARCH_RANGE_LIMIT) return;
        var text = node.nodeValue;
        re.lastIndex = 0;
        var match;
        while ((match = re.exec(text)) !== null) {{
            var range = document.createRange();
            range.setStart(node, match.index);
            range.setEnd(node, match.index + match[0].length);
            ranges.push(range);
            if (ranges.length >= SEARCH_RANGE_LIMIT) break;
            if (match[0].length === 0) re.lastIndex += 1;
        }}
    }});
    return ranges;
}}

function currentRangePaintWindow() {{
    var start = 0;
    var end = Math.min(searchRanges.length, SEARCH_PAINT_LIMIT);
    if (currentMatchIdx >= 0) {{
        start = Math.max(0, currentMatchIdx - Math.floor(SEARCH_PAINT_LIMIT / 2));
        end = Math.min(searchRanges.length, start + SEARCH_PAINT_LIMIT);
    }}
    return {{ start: start, end: end }};
}}

function paintPreviewHighlights() {{
    if (searchRanges.length === 0) {{
        clearHighlights();
        return;
    }}
    var window = currentRangePaintWindow();
    if (supportsCustomHighlight()) {{
        var painted = new Highlight();
        for (var i = window.start; i < window.end; i++) painted.add(searchRanges[i]);
        CSS.highlights.set('previewmd-search', painted);
        if (currentMatchIdx >= 0 && searchRanges[currentMatchIdx]) {{
            var current = new Highlight();
            current.add(searchRanges[currentMatchIdx]);
            CSS.highlights.set('previewmd-search-current', current);
        }} else {{
            CSS.highlights.delete('previewmd-search-current');
        }}
        return;
    }}
    paintFallbackMark();
}}

// Legacy WebKit without the Custom Highlight API: keep the exact match count
// from the range list, but only materialize the current hit in the DOM.
function paintFallbackMark() {{
    var marks = document.querySelectorAll('#content mark.search-highlight');
    marks.forEach(function(m) {{
        var parent = m.parentNode;
        while (m.firstChild) parent.insertBefore(m.firstChild, m);
        parent.removeChild(m);
        parent.normalize();
    }});
    if (currentMatchIdx < 0 || !searchRanges[currentMatchIdx]) return;
    var mark = document.createElement('mark');
    mark.className = 'search-highlight current';
    try {{
        searchRanges[currentMatchIdx].surroundContents(mark);
    }} catch (error) {{
        // Ranges crossing element boundaries cannot be surrounded; the count and
        // scrolling still work, only the paint is skipped.
    }}
}}

function doSearch() {{
    var query = document.getElementById('search-input').value;
    clearHighlights();
    searchMatches = [];
    searchRanges = [];
    currentMatchIdx = -1;
    searchQuery = query;

    if (!query) {{
        document.getElementById('search-count').textContent = '0/0';
        renderEditorHighlights();
        return;
    }}

    if (hasEditor()) {{
        var editor = document.getElementById('editor');
        searchMatches = collectEditorMatches(editor.value, query);
        if (searchMatches.length > 0 && !searchComposing) {{
            currentMatchIdx = 0;
            selectEditorMatch(searchMatches[0].start, searchMatches[0].end);
        }}
        renderEditorHighlights();
        updateSearchCount();
        return;
    }}

    // Preview search never re-renders the document: matches are computed as
    // ranges over the existing DOM and painted by the engine.
    searchRanges = collectPreviewRanges(document.getElementById('content'), query);
    searchMatches = searchRanges;
    if (searchRanges.length > 0) {{
        currentMatchIdx = 0;
        paintPreviewHighlights();
        scrollToRange(0);
    }} else {{
        paintPreviewHighlights();
    }}
    renderEditorHighlights();
    updateSearchCount();
}}

function updateSearchCount() {{
    var count = searchMatches.length;
    var cur = count > 0 ? (currentMatchIdx + 1) : 0;
    document.getElementById('search-count').textContent = cur + '/' + count;
}}

function scrollToRange(index) {{
    if (index < 0 || index >= searchRanges.length) return;
    var content = document.getElementById('content');
    var rectangle = searchRanges[index].getBoundingClientRect();
    var contentRectangle = content.getBoundingClientRect();
    var target = content.scrollTop + (rectangle.top - contentRectangle.top) - (content.clientHeight - rectangle.height) / 2;
    content.scrollTo({{ top: Math.max(0, target), behavior: 'smooth' }});
}}

function scrollToMatch(idx) {{
    if (idx < 0 || idx >= searchRanges.length) return;
    currentMatchIdx = idx;
    paintPreviewHighlights();
    scrollToRange(idx);
}}

function selectEditorMatch(start, end) {{
    var editor = document.getElementById('editor');
    editor.setSelectionRange(start, end);
    var matchOffset = measureEditorOffset(editor, start);
    editor.scrollTop = Math.max(0, matchOffset - editor.clientHeight / 2);
    document.getElementById('search-input').focus({{ preventScroll: true }});
}}

function searchNext() {{
    if (searchMatches.length === 0) return;
    if (hasEditor() && currentMatchIdx === -1) {{
        currentMatchIdx = 0;
    }} else {{
        currentMatchIdx = (currentMatchIdx + 1) % searchMatches.length;
    }}
    if (hasEditor()) {{
        var m = searchMatches[currentMatchIdx];
        selectEditorMatch(m.start, m.end);
        updateCurrentEditorHighlight();
        updateSearchCount();
        return;
    }}
    scrollToMatch(currentMatchIdx);
    updateSearchCount();
}}

function searchPrev() {{
    if (searchMatches.length === 0) return;
    if (hasEditor() && currentMatchIdx === -1) {{
        currentMatchIdx = searchMatches.length - 1;
    }} else {{
        currentMatchIdx = (currentMatchIdx - 1 + searchMatches.length) % searchMatches.length;
    }}
    if (hasEditor()) {{
        var m = searchMatches[currentMatchIdx];
        selectEditorMatch(m.start, m.end);
        updateCurrentEditorHighlight();
        updateSearchCount();
        return;
    }}
    scrollToMatch(currentMatchIdx);
    updateSearchCount();
}}

// ── Keyboard shortcuts ──
document.addEventListener('keydown', function(e) {{
    if (e.isComposing) return;
    var meta = e.metaKey || e.ctrlKey;
    var key = (e.key || '').toLowerCase();
    var editorElement = document.getElementById('editor');
    var editorFocused = editorElement && document.activeElement === editorElement;

    if (editorFocused && meta && key === 'z') {{
        e.preventDefault();
        if (e.shiftKey) redoEditor();
        else undoEditor();
        return;
    }}

    if (meta && e.shiftKey && key === 's') {{
        e.preventDefault();
        saveFileAs();
        return;
    }}

    if (meta && key === 's') {{
        e.preventDefault();
        saveFile();
        return;
    }}

    if (meta && key === 'n') {{
        e.preventDefault();
        newUntitledTab();
        return;
    }}

    if (meta && key === 'r') {{
        e.preventDefault();
        reloadActiveTab();
        return;
    }}

    if (meta && key === 'f') {{
        e.preventDefault();
        openSearch();
        return;
    }}

    if (e.key === 'Escape') {{
        if (document.getElementById('lightbox').classList.contains('open')) {{
            closeLightbox();
            return;
        }}
        if (document.getElementById('export-menu').classList.contains('open')) {{
            closeExportMenu();
            document.getElementById('tab-export').focus();
            return;
        }}
        if (document.getElementById('search-bar').style.display === 'block') {{
            closeSearch();
            return;
        }}
        if (hasEditor()) {{
            setViewMode('preview');
            return;
        }}
    }}

    if (e.key === 'Tab' && document.getElementById('lightbox').classList.contains('open')) {{
        var controls = Array.from(document.querySelectorAll('#lightbox-controls button'));
        var current = controls.indexOf(document.activeElement);
        e.preventDefault();
        controls[(current + (e.shiftKey ? controls.length - 1 : 1)) % controls.length].focus();
        return;
    }}

    if (e.key === 'Enter' && document.getElementById('search-bar').style.display === 'block') {{
        e.preventDefault();
        if (e.shiftKey) searchPrev();
        else searchNext();
        return;
    }}
}});

document.getElementById('search-input').addEventListener('input', function() {{
    clearTimeout(searchInputTimer);
    searchInputTimer = setTimeout(doSearch, SEARCH_DEBOUNCE_MS);
}});
document.getElementById('search-input').addEventListener('compositionstart', function() {{
    searchComposing = true;
}});
document.getElementById('search-input').addEventListener('compositionend', function() {{
    searchComposing = false;
    clearTimeout(searchInputTimer);
    doSearch();
}});
document.getElementById('search-prev').addEventListener('click', function() {{
    searchPrev();
}});
document.getElementById('search-next').addEventListener('click', function() {{
    searchNext();
}});
document.getElementById('search-close').addEventListener('click', function() {{
    closeSearch();
}});

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
    var dropped = Array.prototype.slice.call((e.dataTransfer && e.dataTransfer.files) || []);
    var accepted = dropped.filter(function(file) {{
        var lowerName = (file.name || '').toLowerCase();
        return lowerName.endsWith('.md') || lowerName.endsWith('.txt');
    }});
    if (accepted.length === 0) {{
        if (dropped.length > 0) showStatus('Only .md or .txt files can be opened.');
        return;
    }}

    // The native bridge resolves real paths when pywebview supports it.
    if (window.previewmdNativeDropReady === true) return;

    if (window.pywebview && accepted[0].path) {{
        accepted.forEach(function(file) {{
            callBridge('dropped_file', file.path).catch(function() {{}});
        }});
        return;
    }}

    accepted.forEach(function(file) {{
        var reader = new FileReader();
        reader.onload = function(ev) {{
            addTab(file.name || 'untitled.md', null, ev.target.result);
        }};
        reader.readAsText(file);
    }});
}}

// ── Python bridge ──
function openNewFile() {{
    if (window.pywebview) {{
        window.pywebview.api.open_file();
    }}
}}

// Called from Python when a file is opened via dialog
window.addTabFromPython = function(name, path, content, diskHash) {{
    addTab(name, path, content, diskHash);
}};

// Called from Python when a watched file changes on disk
window.updateTabFromPython = function(path, content, diskHash) {{
    var idx = findTabByPath(path);
    if (idx < 0) return;
    var tab = tabs[idx];
    if (tab.dirty) {{
        markTabConflict(tab, content, diskHash);
        return;
    }}
    applyDiskSnapshot(tab, content, diskHash);
    if (tabs[activeIdx] === tab) showStatus('Reloaded changes from disk.');
}};

// Called from Python when a drag-drop path is available
window.openPathFromPython = function(name, path, content, diskHash) {{
    addTab(name, path, content, diskHash);
}};

// Called from Python after the previous session's tabs have been reopened
window.finishSessionRestore = function(state) {{
    if (!state || typeof state !== 'object') return;
    if (typeof state.active === 'number' && state.active >= 0 && state.active < tabs.length) {{
        activeIdx = state.active;
        renderTabs();
        renderActiveTab();
        syncWindowTitle();
    }}
    if (tabs.length > 0 && ['preview', 'edit', 'split'].indexOf(state.view_mode) >= 0) {{
        setViewMode(state.view_mode);
    }}
    syncSession();
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
            directory=self._app._dialog_directory(),
            allow_multiple=False,
            file_types=("Markdown and Text Files (*.md;*.txt)",)
        )
        if result and result[0]:
            self._app.load_file(result[0])

    def dropped_file(self, filepath):
        """Called from JS when drag-drop provides a file path."""
        if filepath and filepath.lower().endswith((".md", ".txt")) and os.path.isfile(filepath):
            self._app.load_file(filepath)

    def save_session(self, state):
        """Persist the open tab list; the UI owns the tab shape."""
        payload = dict(state) if isinstance(state, dict) else {}
        payload["last_directory"] = self._app._last_directory
        return {"ok": persist_session(payload, self._app._session_path)}

    def stop_watching(self, filepath):
        """Called from JS when a tab is closed."""
        self._app.unwatch_file(filepath)

    def set_title(self, title):
        self._app._window.set_title(title)

    def set_dirty_state(self, dirty):
        """Receive only the aggregate unsaved-work state from the trusted UI."""
        self._app._has_unsaved_changes = dirty is True
        return {"ok": True}

    def save_file(self, filepath, content, expected_hash, force=False):
        try:
            filepath = self._app._normalize_path(filepath)
            watcher = self._app._watchers.get(filepath)
            if watcher is None:
                return {"ok": False, "error": "File is not open for saving."}
            watcher_token = watcher["token"]
            new_hash = atomic_write_utf8(filepath, content, expected_hash, bool(force))
            current_watcher = self._app._watchers.get(filepath)
            if current_watcher is None or current_watcher["token"] != watcher_token:
                return {"ok": False, "error": "File watcher changed while saving."}
            current_watcher["hash"] = new_hash
            current_watcher["signature"] = watched_file_signature(filepath)
            return {"ok": True, "hash": new_hash}
        except SaveConflictError as error:
            return {
                "ok": False,
                "conflict": True,
                "disk_hash": error.disk_hash,
                "error": "File changed on disk; autosave was cancelled.",
            }
        except Exception as e:
            print(f"Error saving file: {e}", file=sys.stderr)
            return {"ok": False, "error": f"Save failed: {e}"}

    def reload_file(self, filepath):
        """Read a watched file from disk for an explicit reload request."""
        try:
            filepath = self._app._normalize_path(filepath)
        except Exception:
            return {"ok": False, "error": "Invalid file path."}
        watcher = self._app._watchers.get(filepath)
        if watcher is None:
            return {"ok": False, "error": "File is not open in a tab."}
        try:
            content, new_hash = read_utf8_with_hash(filepath)
        except FileNotFoundError:
            return {"ok": False, "missing": True, "error": "File no longer exists on disk."}
        except Exception as error:
            print(f"Error reloading file: {error}", file=sys.stderr)
            return {"ok": False, "error": "File could not be read."}
        current_watcher = self._app._watchers.get(filepath)
        if current_watcher is not None and current_watcher["token"] == watcher["token"]:
            current_watcher["hash"] = new_hash
            current_watcher["signature"] = watched_file_signature(filepath)
        return {"ok": True, "path": filepath, "content": content, "hash": new_hash}

    def save_file_as(self, suggested_name, content, current_path=None):
        """Write to a path confirmed in the native SAVE dialog and watch it."""
        try:
            suggested = safe_save_filename(suggested_name)
            initial_directory = self._app._dialog_directory()
            try:
                result = self._app._window.create_file_dialog(
                    webview.FileDialog.SAVE,
                    directory=initial_directory,
                    save_filename=suggested,
                    file_types=("Markdown and Text Files (*.md;*.txt)",),
                )
            except TypeError:
                # Older pywebview releases accept these values positionally.
                result = self._app._window.create_file_dialog(
                    webview.FileDialog.SAVE, initial_directory, False, suggested,
                    ("Markdown and Text Files (*.md;*.txt)",),
                )
            if not result:
                return {"ok": False, "cancelled": True}
            selected_path = result if isinstance(result, str) else result[0]
            if not selected_path:
                return {"ok": False, "cancelled": True}
            selected_path = ensure_document_extension(selected_path)
            replaced_path = self._app._normalize_path(current_path) if current_path else None
            new_hash = self._app.save_document(selected_path, content, replaced_path)
            normalized = self._app._normalize_path(selected_path)
            return {
                "ok": True,
                "path": normalized,
                "display_path": display_path(normalized),
                "name": os.path.basename(normalized),
                "hash": new_hash,
            }
        except Exception as error:
            print(f"Error saving file as: {error}", file=sys.stderr)
            return {"ok": False, "error": f"Save failed: {error}"}

    def open_external_url(self, url):
        try:
            value = str(url or "").strip()
            if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value):
                return {"ok": False, "error": "Unsafe external URL."}
            parsed = urlparse(value)
            if parsed.scheme.lower() not in ("http", "https", "mailto"):
                return {"ok": False, "error": "Unsupported external URL scheme."}
            if parsed.scheme.lower() in ("http", "https") and not parsed.netloc:
                return {"ok": False, "error": "External URL has no host."}
            return {"ok": bool(webbrowser.open(value))}
        except Exception as error:
            print(f"Error opening external URL: {error}", file=sys.stderr)
            return {"ok": False, "error": "Could not open external URL."}

    def export_html(self, title, rendered_html, stylesheet):
        """Write only to a path returned by the native SAVE dialog."""
        try:
            suggested_name = safe_export_filename(title)
            try:
                result = self._app._window.create_file_dialog(
                    webview.FileDialog.SAVE,
                    save_filename=suggested_name,
                    file_types=("HTML Files (*.html;*.htm)",),
                )
            except TypeError:
                # Older pywebview releases accept these values positionally.
                result = self._app._window.create_file_dialog(
                    webview.FileDialog.SAVE, "", False, suggested_name,
                    ("HTML Files (*.html;*.htm)",),
                )
            if not result:
                return {"ok": False, "cancelled": True}
            selected_path = result if isinstance(result, str) else result[0]
            if not selected_path:
                return {"ok": False, "cancelled": True}
            document = build_standalone_html(title, rendered_html, stylesheet)
            atomic_export_utf8(selected_path, document)
            return {"ok": True}
        except Exception as error:
            print(f"Error exporting HTML: {error}", file=sys.stderr)
            return {"ok": False, "error": f"Export failed: {error}"}

    def _open_document(self, document_path):
        if not document_path:
            raise ImagePathRequiredError("This tab has no file path; image import is unavailable.")
        normalized = self._app._normalize_path(document_path)
        if normalized not in self._app._watchers:
            raise ImageError("The document is not open as a path-backed tab.")
        return normalized

    def pick_image(self, document_path):
        try:
            document_path = self._open_document(document_path)
            result = self._app._window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=self._app._dialog_directory(),
                allow_multiple=False,
                file_types=("Images (*.png;*.jpg;*.jpeg;*.gif;*.webp)",),
            )
            if not result or not result[0]:
                return {"ok": False, "cancelled": True}
            relative_path = import_image_file(document_path, result[0])
            return {"ok": True, "path": relative_path}
        except ImagePathRequiredError as error:
            return {"ok": False, "needs_path": True, "error": str(error)}
        except ImageError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            print(f"Error importing image: {error}", file=sys.stderr)
            return {"ok": False, "error": "Image import failed."}

    def import_image_path(self, document_path, source_path):
        try:
            document_path = self._open_document(document_path)
            relative_path = import_image_file(document_path, source_path)
            return {"ok": True, "path": relative_path}
        except ImagePathRequiredError as error:
            return {"ok": False, "needs_path": True, "error": str(error)}
        except ImageError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            print(f"Error importing dropped image: {error}", file=sys.stderr)
            return {"ok": False, "error": "Image import failed."}

    def import_image_data(self, document_path, filename, data_url):
        try:
            document_path = self._open_document(document_path)
            header, separator, encoded = str(data_url or "").partition(",")
            allowed_headers = {
                "data:image/png;base64",
                "data:image/jpeg;base64",
                "data:image/gif;base64",
                "data:image/webp;base64",
                "data:application/octet-stream;base64",
            }
            if not separator or header.lower() not in allowed_headers:
                raise ImageError("Only PNG, JPEG, GIF, and WebP images are supported.")
            max_encoded_length = ((MAX_IMAGE_BYTES + 2) // 3) * 4
            if len(encoded) > max_encoded_length:
                raise ImageError("Image is larger than the 20 MB limit.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ImageError("Image data is invalid.") from error
            relative_path = import_image_bytes(document_path, filename, data)
            return {"ok": True, "path": relative_path}
        except ImagePathRequiredError as error:
            return {"ok": False, "needs_path": True, "error": str(error)}
        except ImageError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            print(f"Error importing pasted image: {error}", file=sys.stderr)
            return {"ok": False, "error": "Image import failed."}

    def resolve_image(self, document_path, source):
        try:
            document_path = self._open_document(document_path)
            data_url = self._app.resolve_image_data_url(document_path, source)
            if data_url is None:
                return {"ok": False, "error": "Unsupported, missing, or oversized local image."}
            return {"ok": True, "data_url": data_url}
        except ImagePathRequiredError as error:
            return {"ok": False, "needs_path": True, "error": str(error)}
        except ImageError as error:
            return {"ok": False, "error": str(error)}
        except Exception as error:
            print(f"Error resolving image: {error}", file=sys.stderr)
            return {"ok": False, "error": "Image could not be resolved."}


class PreviewApp:
    def __init__(self, session_path=None):
        self._window = None
        self._watchers = {}     # {filepath: {"observer": ..., "directory": ..., "hash": ..., "token": ...}}
        self._directory_watches = {}  # {directory: {"watch": ..., "count": ...}}
        self._observer = None
        self._watcher_generation = 0
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self._pending_files = []
        self._has_unsaved_changes = False
        self._session_path = session_path or session_file_path()
        self._session = load_session(self._session_path)
        self._last_directory = self._session["last_directory"]
        self._image_cache = OrderedDict()
        self._image_cache_bytes = 0
        self._api = Api(self)

    def _dialog_directory(self):
        """Return a starting folder for dialogs, empty for the system default."""
        if self._last_directory and os.path.isdir(self._last_directory):
            return self._last_directory
        return ""

    def resolve_image_data_url(self, document_path, source):
        """Resolve a local image, reusing encoded data until the file changes."""
        image_path = resolve_local_image_path(document_path, source)
        if image_path is None:
            return None
        try:
            file_stat = image_path.stat()
        except OSError:
            return None
        key = (os.fspath(image_path), file_stat.st_mtime_ns, file_stat.st_size)
        cached = self._image_cache.get(key)
        if cached is not None:
            self._image_cache.move_to_end(key)
            return cached
        data_url = image_data_url(image_path)
        if data_url is None:
            return None
        self._image_cache[key] = data_url
        self._image_cache_bytes += len(data_url)
        while self._image_cache and (
            len(self._image_cache) > IMAGE_CACHE_MAX_ENTRIES
            or self._image_cache_bytes > IMAGE_CACHE_MAX_BYTES
        ):
            _, evicted = self._image_cache.popitem(last=False)
            self._image_cache_bytes -= len(evicted)
        return data_url

    def _hash_content(self, text):
        return content_hash(text)

    def _normalize_path(self, filepath):
        return os.path.realpath(os.path.abspath(filepath))

    def load_file(self, filepath):
        """Read a .md file, send to JS as a new tab, and start watching."""
        filepath = self._normalize_path(filepath)
        if not filepath.lower().endswith((".md", ".txt")) or not os.path.exists(filepath):
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
        disk_hash = self._hash_content(content)
        escaped_hash = json.dumps(disk_hash)

        existing_watcher = self._watchers.get(filepath)
        if existing_watcher is None:
            self._start_watching(filepath, content)
        self._last_directory = os.path.dirname(filepath)

        # Send to JS
        self._window.evaluate_js(
            f"window.addTabFromPython({escaped_name}, {escaped_path}, {escaped_content}, {escaped_hash})"
        )
        if existing_watcher is not None:
            # Update watcher state only after JS has received the reopened
            # snapshot; failed delivery must not seed unseen disk content.
            current_watcher = self._watchers.get(filepath)
            if current_watcher is existing_watcher:
                current_watcher["hash"] = disk_hash
                current_watcher["signature"] = watched_file_signature(filepath)

    def _start_watching(self, filepath, content):
        watched_dir = os.path.dirname(filepath)
        self._watcher_generation += 1
        token = self._watcher_generation
        handler = FileChangeHandler(filepath, token, callback=self._on_file_changed)
        observer = self._shared_observer()
        directory_entry = self._directory_watches.get(watched_dir)
        if directory_entry is None:
            watch = observer.schedule(handler, watched_dir, recursive=False)
            directory_entry = {"watch": watch, "count": 1}
            self._directory_watches[watched_dir] = directory_entry
        else:
            observer.add_handler_for_watch(handler, directory_entry["watch"])
            directory_entry["count"] += 1

        self._watchers[filepath] = {
            "observer": observer,
            "directory": watched_dir,
            "handler": handler,
            "hash": self._hash_content(content),
            "signature": watched_file_signature(filepath),
            "token": token,
            "debounce": None,
        }
        self._ensure_polling()

    def _shared_observer(self):
        if self._observer is None:
            self._observer = Observer()
            self._observer.daemon = True
            self._observer.start()
        return self._observer

    def _ensure_polling(self):
        """Start the slow stat poll that backstops missed filesystem events."""
        if self._window is None or self._poll_thread is not None:
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_watched_files, daemon=True)
        self._poll_thread.start()

    def _poll_watched_files(self):
        while not self._poll_stop.wait(WATCHER_POLL_INTERVAL_SECONDS):
            self._check_watched_files_once()

    def _check_watched_files_once(self):
        """Reload watched files whose size or mtime changed since the last check."""
        for filepath, watcher in list(self._watchers.items()):
            signature = watched_file_signature(filepath)
            if signature is None or signature == watcher.get("signature"):
                continue
            watcher["signature"] = signature
            self._on_file_changed(filepath, watcher["token"])

    def unwatch_file(self, filepath):
        filepath = self._normalize_path(filepath)
        watcher = self._watchers.pop(filepath, None)
        if watcher is None:
            return
        timer = watcher.get("debounce")
        if timer is not None:
            timer.cancel()
        observer = watcher.get("observer")
        watched_dir = watcher.get("directory")
        directory_entry = self._directory_watches.get(watched_dir) if watched_dir else None
        if observer is None or directory_entry is None:
            return
        try:
            observer.remove_handler_for_watch(watcher["handler"], directory_entry["watch"])
        except Exception as error:
            print(f"Could not detach watcher for {filepath}: {error}", file=sys.stderr)
        directory_entry["count"] -= 1
        if directory_entry["count"] > 0:
            return
        del self._directory_watches[watched_dir]
        try:
            observer.unschedule(directory_entry["watch"])
        except Exception as error:
            print(f"Could not stop watching {watched_dir}: {error}", file=sys.stderr)

    def save_document(self, filepath, content, replaced_path=None):
        """Write a user-confirmed path and make it an auto-saving document."""
        filepath = self._normalize_path(filepath)
        if not filepath.lower().endswith((".md", ".txt")):
            raise ValueError("Only .md and .txt files can be saved.")
        if filepath in self._watchers and filepath != replaced_path:
            raise ValueError("That file is already open in another tab.")
        directory = os.path.dirname(filepath)
        if directory and not os.path.isdir(directory):
            raise ValueError("The selected folder no longer exists.")
        new_hash = atomic_export_utf8(filepath, content)
        watcher = self._watchers.get(filepath)
        if watcher is None:
            self._start_watching(filepath, content)
        else:
            watcher["hash"] = new_hash
            watcher["signature"] = watched_file_signature(filepath)
        self._last_directory = os.path.dirname(filepath)
        return new_hash

    def _on_file_changed(self, filepath, token):
        """Debounce a watcher event without blocking the observer thread."""
        watcher = self._watchers.get(filepath)
        if watcher is None or watcher["token"] != token:
            return
        timer = watcher.get("debounce")
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(0.15, self._apply_file_change, args=(filepath, token))
        timer.daemon = True
        watcher["debounce"] = timer
        timer.start()

    def _apply_file_change(self, filepath, token):
        """Reload a watched file after the debounce unless it is stale."""
        watcher = self._watchers.get(filepath)
        if watcher is None or watcher["token"] != token:
            return
        watcher["debounce"] = None

        try:
            content, new_hash = read_utf8_with_hash(filepath)
        except Exception:
            return

        watcher = self._watchers.get(filepath)
        if watcher is None or watcher["token"] != token:
            return
        if new_hash == watcher["hash"]:
            return

        escaped_path = json.dumps(filepath)
        escaped_content = json.dumps(content)
        escaped_hash = json.dumps(new_hash)
        self._window.evaluate_js(
            f"window.updateTabFromPython({escaped_path}, {escaped_content}, {escaped_hash})"
        )
        watcher = self._watchers.get(filepath)
        if watcher is not None and watcher["token"] == token:
            watcher["hash"] = new_hash

    def handle_native_drop(self, event):
        """Open every usable path a pywebview native drop reported."""
        try:
            files = (event or {}).get("dataTransfer", {}).get("files") or []
        except AttributeError:
            files = []
        opened = 0
        for item in files:
            if not isinstance(item, dict):
                continue
            path = item.get("pywebviewFullPath")
            if not path or not path.lower().endswith((".md", ".txt")) or not os.path.isfile(path):
                continue
            self.load_file(path)
            opened += 1
        if opened == 0 and files:
            self._show_ui_status("Only .md or .txt files can be opened.")

    def _show_ui_status(self, message):
        try:
            self._window.evaluate_js(f"window.showStatus({json.dumps(message)})")
        except Exception:
            pass

    def _register_native_drop(self):
        """Let pywebview resolve real drop paths instead of the FileReader fallback."""
        try:
            body = self._window.dom.get_element("body")
            if body is None:
                return
            body.on("drop", DOMEventHandler(self.handle_native_drop, prevent_default=True))
            self._window.evaluate_js("window.previewmdNativeDropReady = true")
        except Exception as error:
            print(f"Native file drop is unavailable: {error}", file=sys.stderr)

    def _on_loaded(self):
        """Open the CLI file, a file:// URL, or the previous session's tabs."""
        opened = False
        try:
            url = self._window.get_current_url()
            if url and url.startswith("file://"):
                parsed_url = urlparse(url)
                filepath = unquote(parsed_url.path)
                if os.path.isfile(filepath) and filepath.lower().endswith((".md", ".txt")):
                    self.load_file(filepath)
                    opened = True
        except Exception:
            pass

        if self._pending_files:
            pending = self._pending_files
            self._pending_files = []
            for path in pending:
                self.load_file(path)
            opened = True

        self._register_native_drop()

        if not opened:
            self._restore_session()

    def _restore_session(self):
        paths, active = plan_session_restore(self._session["tabs"], self._session["active"])
        for path in paths:
            self.load_file(path)
        state = json.dumps({"active": active, "view_mode": self._session["view_mode"]})
        try:
            self._window.evaluate_js(f"window.finishSessionRestore({state})")
        except Exception as error:
            print(f"Could not restore the previous session: {error}", file=sys.stderr)

    def _on_closing(self):
        """Cancel native close unless the user confirms discarding unsaved work."""
        if not self._has_unsaved_changes:
            return True
        try:
            return bool(self._window.create_confirmation_dialog(
                "Unsaved Changes",
                "PreviewMD still has unsaved edits or a save in progress. "
                "Close the window and discard them?",
            ))
        except Exception as error:
            print(f"Error showing close confirmation: {error}", file=sys.stderr)
            return False

    def run(self, filepaths=None):
        self._pending_files = list(filepaths or [])
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
        self._window.events.closing += self._on_closing

        webview.start(debug=False)
        self._cleanup()

    def _cleanup(self):
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=1)
            self._poll_thread = None
        for path in list(self._watchers.keys()):
            self.unwatch_file(path)
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=1)
            self._observer = None


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    filepaths = []
    for candidate in sys.argv[1:]:
        if candidate.startswith("-"):
            continue
        if not os.path.isfile(candidate):
            print(f"File not found: {candidate}", file=sys.stderr)
            sys.exit(1)
        if not candidate.lower().endswith((".md", ".txt")):
            print(f"Not a Markdown or text file: {candidate}", file=sys.stderr)
            sys.exit(1)
        filepaths.append(candidate)

    app = PreviewApp()
    app.run(filepaths)


if __name__ == "__main__":
    main()
