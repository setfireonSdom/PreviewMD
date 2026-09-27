"""Save failures must read as something a person can act on.

The original message was the raw Python error, which named the internal
temporary file:

    Save failed: [Errno 13] Permission denied: '/…/.note.md.2t8kozk5.tmp'

The user has never seen that name and cannot do anything with it, and the
message never said what to do instead. A read-only folder, a full disk, and a
vanished folder all need different responses.
"""

import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path

import preview
from preview import describe_save_error


class SaveErrorMessageTests(unittest.TestCase):
    def test_permission_problems_name_the_folder_and_the_next_step(self):
        for code in (errno.EACCES, errno.EPERM, errno.EROFS):
            with self.subTest(code=code):
                message = describe_save_error(
                    OSError(code, os.strerror(code)), "/Users/me/Documents/note.md"
                )
                self.assertIn("Save As", message)
                self.assertNotIn("[Errno", message)

    def test_a_full_disk_is_named_as_such(self):
        for code in (errno.ENOSPC, errno.EDQUOT):
            with self.subTest(code=code):
                message = describe_save_error(
                    OSError(code, os.strerror(code)), "/Users/me/Documents/note.md"
                )
                self.assertIn("no space left", message)

    def test_a_folder_in_the_way_is_explained(self):
        message = describe_save_error(
            OSError(errno.EISDIR, os.strerror(errno.EISDIR)), "/Users/me/Documents/note.md"
        )
        self.assertIn("folder", message)
        self.assertIn("note.md", message)

    def test_a_vanished_folder_is_distinguished_from_a_read_only_one(self):
        message = describe_save_error(
            OSError(errno.ENOENT, os.strerror(errno.ENOENT)), "/Users/me/Documents/note.md"
        )
        self.assertIn("no longer exists", message)

    def test_the_internal_temporary_file_never_reaches_the_user(self):
        # This is the defect being fixed: the raw error names ".note.md.2t8kozk5.tmp".
        real = None
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "note.md"
            document.write_text("original\n", encoding="utf-8")
            os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
            try:
                with self.assertRaises(OSError) as context:
                    # force skips the hash check so the failure comes from
                    # creating the temporary file, which is what leaks the name.
                    preview.atomic_write_utf8(str(document), "changed\n", None, True)
                real = str(context.exception)
            finally:
                os.chmod(directory, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        # The raw error does mention the temporary file...
        self.assertIn(".tmp", real)
        # ...and the message the user sees does not.
        message = describe_save_error(OSError(errno.EACCES, "Permission denied"), "/x/note.md")
        self.assertNotIn(".tmp", message)

    def test_an_unrecognised_error_still_keeps_the_system_wording(self):
        message = describe_save_error(
            OSError(errno.EBADF, os.strerror(errno.EBADF)), "/x/note.md"
        )
        self.assertIn("note.md", message)
        self.assertIn("Bad file descriptor", message)

    def test_a_non_os_error_does_not_crash(self):
        message = describe_save_error(ValueError("something odd"), "/x/note.md")
        self.assertIn("note.md", message)
        self.assertIn("previewmd.log", message)

    def test_the_raw_traceback_still_reaches_the_log(self):
        class FakeElement:
            def on(self, event, callback):
                self.callback = callback

        class FakeWindow:
            def __init__(self):
                self.dom = type("D", (), {"get_element": lambda self, s: FakeElement()})()
                self.titles = []

            def evaluate_js(self, script):
                pass

            def set_title(self, title):
                self.titles.append(title)

        with tempfile.TemporaryDirectory() as directory:
            log_dir = os.path.join(directory, "logs")
            preview.configure_logging(log_dir)
            self.addCleanup(preview.configure_logging, tempfile.mkdtemp())
            document = Path(directory) / "note.md"
            document.write_text("original\n", encoding="utf-8")

            app = preview.PreviewApp(session_path=os.path.join(directory, "session.json"))
            app._window = FakeWindow()
            app.load_file(str(document))
            os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
            try:
                result = preview.Api(app).save_file(str(document), "changed\n", None, True)
            finally:
                os.chmod(directory, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            self.assertFalse(result["ok"])
            self.assertEqual(result["retry_as"], "save_as")
            self.assertIn("Save As", result["error"])
            with open(os.path.join(log_dir, "previewmd.log"), encoding="utf-8") as handle:
                logged = handle.read()
            # The log is where the temporary path belongs.
            self.assertIn(".tmp", logged)
            self.assertIn("Traceback", logged)


if __name__ == "__main__":
    unittest.main()
