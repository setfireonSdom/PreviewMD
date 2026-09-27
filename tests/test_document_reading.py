import codecs
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from preview import (
    MAX_DOCUMENT_BYTES,
    DocumentReadError,
    PreviewApp,
    content_hash,
    decode_document,
    read_document,
    read_utf8_with_hash,
)


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)

    def status_messages(self):
        messages = []
        for script in self.scripts:
            if script.startswith("window.showStatus("):
                messages.append(json.loads(script[len("window.showStatus("):-1]))
        return messages


class DocumentDecodingTests(unittest.TestCase):
    def test_plain_utf8_is_read_without_a_bom_marker(self):
        text, encoding = decode_document("标题\n正文".encode())
        self.assertEqual(text, "标题\n正文")
        self.assertEqual(encoding, "utf-8-sig")

    def test_utf8_with_bom_loses_the_bom(self):
        text, encoding = decode_document("标题".encode("utf-8-sig"))
        self.assertEqual(text, "标题")
        self.assertFalse(text.startswith("﻿"))
        self.assertEqual(encoding, "utf-8-sig")

    def test_gb18030_is_accepted_as_a_fallback(self):
        text, encoding = decode_document("第一章 你好，世界".encode("gb18030"))
        self.assertEqual(encoding, "gb18030")
        self.assertIn("第一章", text)

    def test_binary_content_is_rejected_with_a_readable_reason(self):
        with self.assertRaises(DocumentReadError) as context:
            decode_document(b"\x00\x01\x02binary")
        self.assertIn("二进制", str(context.exception))

    def test_undecodable_content_is_rejected_with_a_readable_reason(self):
        with self.assertRaises(DocumentReadError) as context:
            decode_document(bytes(range(1, 200)))
        self.assertIn("编码", str(context.exception))

    def test_utf16_with_a_byte_order_mark_is_read_not_mistaken_for_binary(self):
        # Windows Notepad saves UTF-16, which is full of NUL bytes and used to be
        # reported as "this looks like a binary file".
        for bom, codec in ((codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
            with self.subTest(bom=bom):
                text, detected = decode_document(bom + "第1章 标题".encode(codec))
                self.assertEqual(detected, "utf-16")
                self.assertIn("第1章", text)

    def test_utf32_with_a_byte_order_mark_is_read(self):
        text, detected = decode_document(codecs.BOM_UTF32_LE + "第1章".encode("utf-32-le"))
        self.assertEqual(detected, "utf-32")
        self.assertIn("第1章", text)

    def test_real_binary_is_still_rejected(self):
        with self.assertRaises(DocumentReadError) as context:
            decode_document(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        self.assertIn("二进制", str(context.exception))

    def test_document_read_error_is_an_os_error(self):
        # File-IO callers (session persistence) only catch OSError.
        self.assertTrue(issubclass(DocumentReadError, OSError))


class ReadDocumentTests(unittest.TestCase):
    def test_reads_text_and_reports_the_encoding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            path.write_bytes("你好".encode("gb18030"))
            text, encoding = read_document(path)
            self.assertEqual(text, "你好")
            self.assertEqual(encoding, "gb18030")

    def test_a_missing_file_still_raises_file_not_found(self):
        # Reload distinguishes "deleted" from "unreadable".
        with self.assertRaises(FileNotFoundError):
            read_document("/nonexistent/definitely-missing.md")

    def test_oversized_documents_are_refused_with_a_readable_reason(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "big.md"
            path.write_bytes(b"a" * 4096)
            with mock.patch("preview.MAX_DOCUMENT_BYTES", 1024):
                with self.assertRaises(DocumentReadError) as context:
                    read_document(path)
            self.assertIn("文件过大", str(context.exception))
        self.assertGreater(MAX_DOCUMENT_BYTES, 1024 * 1024)

    def test_hash_is_stable_across_source_encodings(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            utf8 = Path(temporary_directory) / "a.md"
            gb = Path(temporary_directory) / "b.md"
            utf8.write_bytes("同样的内容".encode())
            gb.write_bytes("同样的内容".encode("gb18030"))
            self.assertEqual(
                read_utf8_with_hash(utf8)[1],
                read_utf8_with_hash(gb)[1],
            )
            self.assertEqual(read_utf8_with_hash(utf8)[1], content_hash("同样的内容"))


class LoadFileFeedbackTests(unittest.TestCase):
    def make_app(self, window):
        app = PreviewApp()
        app._window = window
        return app

    def seed_watcher(self, app, path, content):
        normalized = app._normalize_path(path)
        app._watchers[normalized] = {
            "observer": None,
            "hash": content_hash(content),
            "token": 1,
        }
        return normalized

    def test_a_missing_file_is_reported_to_the_user(self):
        window = FakeWindow()
        app = self.make_app(window)
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "gone.md"
            app.load_file(missing)
        self.assertTrue(any("文件不存在" in m for m in window.status_messages()), window.scripts)

    def test_a_binary_file_is_reported_to_the_user(self):
        window = FakeWindow()
        app = self.make_app(window)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "blob.md"
            path.write_bytes(b"\x00\x01\x02\x03binary")
            app.load_file(path)
        self.assertTrue(any("二进制" in m for m in window.status_messages()), window.scripts)

    def test_an_unsupported_extension_is_ignored_silently(self):
        window = FakeWindow()
        app = self.make_app(window)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "photo.png"
            path.write_bytes(b"\x89PNG")
            app.load_file(path)
        self.assertEqual(window.status_messages(), [])

    def test_a_gb18030_document_opens_and_warns_about_saving_as_utf8(self):
        window = FakeWindow()
        app = self.make_app(window)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            text = "第一章 你好，世界"
            path.write_bytes(text.encode("gb18030"))
            self.seed_watcher(app, path, text)
            app.load_file(path)

        self.assertTrue(any("window.addTabFromPython" in s for s in window.scripts))
        self.assertTrue(any("GB18030" in m or "gb18030" in m for m in window.status_messages()))
        self.assertTrue(any("UTF-8" in m for m in window.status_messages()))

    def test_a_utf8_document_does_not_warn_about_encoding(self):
        window = FakeWindow()
        app = self.make_app(window)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "note.md"
            text = "# 标题"
            path.write_bytes(text.encode("utf-8"))
            self.seed_watcher(app, path, text)
            app.load_file(path)

        self.assertEqual(window.status_messages(), [])


if __name__ == "__main__":
    unittest.main()
