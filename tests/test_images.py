import tempfile
import unittest
from pathlib import Path
from unittest import mock

from preview import (
    MAX_IMAGE_BYTES,
    Api,
    ImageError,
    PreviewApp,
    _validated_image_bytes,
    image_data_url,
    import_image_bytes,
    local_image_data_url,
    resolve_local_image_path,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-payload"
GIF_BYTES = b"GIF89a" + b"test-payload"


class ImageImportTests(unittest.TestCase):
    def test_import_creates_assets_and_uses_collision_suffix(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            document = Path(temporary_directory) / "note.md"
            document.write_text("# Note", encoding="utf-8")

            first = import_image_bytes(document, "diagram.png", PNG_BYTES)
            second = import_image_bytes(document, "diagram.png", PNG_BYTES)

            self.assertEqual(first, "assets/diagram.png")
            self.assertEqual(second, "assets/diagram-2.png")
            self.assertEqual((document.parent / first).read_bytes(), PNG_BYTES)
            self.assertEqual((document.parent / second).read_bytes(), PNG_BYTES)

    def test_import_preserves_safe_unicode_and_spaces(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            document = Path(temporary_directory) / "笔记.md"
            document.touch()

            relative_path = import_image_bytes(document, "示意 图.gif", GIF_BYTES)

            self.assertEqual(relative_path, "assets/示意 图.gif")
            self.assertTrue((document.parent / "assets" / "示意 图.gif").is_file())

    def test_unsupported_and_oversized_images_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            document = Path(temporary_directory) / "note.md"
            document.touch()
            with self.assertRaises(ImageError):
                import_image_bytes(document, "drawing.svg", b"<svg></svg>")
        with self.assertRaises(ImageError):
            _validated_image_bytes(b"x" * (MAX_IMAGE_BYTES + 1), "large.png")

    def test_pathless_import_is_rejected_without_creating_assets(self):
        with self.assertRaises(ImageError):
            import_image_bytes(None, "image.png", PNG_BYTES)


class ImageBridgeRequiresPathTests(unittest.TestCase):
    def make_api(self):
        app = type("FakeApp", (), {})()
        app._window = None
        app._normalize_path = lambda path: path
        app._watchers = {}
        return Api(app)

    def test_pathless_image_requests_are_marked_needs_path(self):
        api = self.make_api()
        responses = (
            api.pick_image(None),
            api.import_image_path("", "/tmp/image.png"),
            api.import_image_data(None, "clip.png", "data:image/png;base64,AAAA"),
            api.resolve_image(None, "assets/a.png"),
        )
        for response in responses:
            self.assertFalse(response["ok"])
            self.assertTrue(response["needs_path"])

    def test_unknown_document_path_is_not_reported_as_needs_path(self):
        response = self.make_api().resolve_image("/tmp/not-open.md", "assets/a.png")
        self.assertFalse(response["ok"])
        self.assertNotIn("needs_path", response)


class ImageDataCacheTests(unittest.TestCase):
    def make_app(self, temporary_directory):
        return PreviewApp(session_path=str(Path(temporary_directory) / "session.json"))

    def test_repeated_resolution_reuses_encoded_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            assets = root / "assets"
            assets.mkdir()
            (assets / "shot.png").write_bytes(PNG_BYTES)
            app = self.make_app(temporary_directory)

            with mock.patch("preview.image_data_url", wraps=image_data_url) as encoder:
                first = app.resolve_image_data_url(str(document), "assets/shot.png")
                second = app.resolve_image_data_url(str(document), "assets/shot.png")

            self.assertEqual(first, second)
            self.assertEqual(encoder.call_count, 1)

    def test_changed_image_is_reencoded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            assets = root / "assets"
            assets.mkdir()
            image = assets / "shot.png"
            image.write_bytes(PNG_BYTES)
            app = self.make_app(temporary_directory)
            first = app.resolve_image_data_url(str(document), "assets/shot.png")

            image.write_bytes(PNG_BYTES + b"more")
            second = app.resolve_image_data_url(str(document), "assets/shot.png")

            self.assertNotEqual(first, second)
            self.assertEqual(len(app._image_cache), 2)

    def test_cache_evicts_entries_over_the_byte_budget(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            assets = root / "assets"
            assets.mkdir()
            (assets / "one.png").write_bytes(PNG_BYTES)
            (assets / "two.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"other-image!")
            app = self.make_app(temporary_directory)
            first_url = image_data_url(assets / "one.png")

            with mock.patch("preview.IMAGE_CACHE_MAX_BYTES", len(first_url)):
                app.resolve_image_data_url(str(document), "assets/one.png")
                app.resolve_image_data_url(str(document), "assets/two.png")

            self.assertEqual(len(app._image_cache), 1)
            self.assertEqual(app._image_cache_bytes, len(app._image_cache[next(iter(app._image_cache))]))

    def test_failed_resolution_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            app = self.make_app(temporary_directory)

            self.assertIsNone(app.resolve_image_data_url(str(document), "assets/missing.png"))
            self.assertEqual(len(app._image_cache), 0)


class ImageResolutionTests(unittest.TestCase):
    def test_relative_percent_encoded_unicode_space_and_suffix_resolve(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "文档.md"
            document.touch()
            assets = root / "assets"
            assets.mkdir()
            image = assets / "示意 图.png"
            image.write_bytes(PNG_BYTES)

            resolved = resolve_local_image_path(
                document, "assets/%E7%A4%BA%E6%84%8F%20%E5%9B%BE.png?raw=1#preview"
            )
            data_url = local_image_data_url(document, "assets/示意 图.png")

            self.assertEqual(resolved, image.resolve())
            self.assertTrue(data_url.startswith("data:image/png;base64,"))

    def test_absolute_and_file_url_resolve_but_pathless_does_not(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            image = root / "photo.gif"
            image.write_bytes(GIF_BYTES)

            self.assertEqual(resolve_local_image_path(document, str(image)), image.resolve())
            self.assertEqual(resolve_local_image_path(document, image.as_uri()), image.resolve())
            self.assertIsNone(resolve_local_image_path(None, str(image)))

    def test_spoofed_or_unsupported_local_files_do_not_render(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document = root / "note.md"
            document.touch()
            fake_png = root / "fake.png"
            fake_png.write_bytes(b"<svg></svg>")
            svg = root / "drawing.svg"
            svg.write_text("<svg></svg>", encoding="utf-8")

            self.assertIsNone(local_image_data_url(document, "fake.png"))
            self.assertIsNone(resolve_local_image_path(document, "drawing.svg"))


if __name__ == "__main__":
    unittest.main()
