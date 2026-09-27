"""The repository is public, so it must never carry the maintainer's own data.

This is a guard rather than a one-off check. The risk is not hypothetical: the
benchmark harness was pointed at real personal documents during development, and
a single `--file` path pasted into a docstring or a fixture would be published
forever. The checks below compare the tracked tree against the machine that is
running them, so they catch the local user's real home directory and account name
rather than a hardcoded guess.
"""

import getpass
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Names that are too generic to be identifying, and that show up as ordinary
# words in prose or code, so matching them would only produce noise.
GENERIC_NAMES = {
    "user", "users", "runner", "test", "root", "admin", "mac", "shared",
    "default", "guest", "build", "actions", "nobody", "owner", "home",
}

TEXT_SUFFIXES = {".py", ".md", ".toml", ".cfg", ".ini", ".txt", ".yml", ".yaml", ".sh", ".spec", ".json"}

# Runtime state that belongs on the user's disk and never in version control.
RUNTIME_PATTERNS = ("session.json", "settings.json", ".log", "baseline.json")


def tracked_files():
    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def read_tracked(relative):
    try:
        return (PROJECT_ROOT / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def local_user_name():
    try:
        name = getpass.getuser()
    except Exception:
        return ""
    return "" if name.lower() in GENERIC_NAMES or len(name) < 4 else name


@unittest.skipUnless(tracked_files(), "not a git checkout")
class NoPersonalDataTests(unittest.TestCase):
    def test_no_tracked_file_contains_the_real_home_directory(self):
        # This is the general guard, and it deliberately does not hardcode the
        # names of any private directory: the corpora this was developed against
        # live under the home directory, so an absolute path to any of them is
        # caught here. Naming them would only publish the layout while checking
        # a fraction of it.
        home = os.path.expanduser("~")
        # macOS resolves /var to /private/var, so compare the resolved form too.
        resolved = os.path.realpath(home)
        offenders = []
        for relative in tracked_files():
            if Path(relative).suffix.lower() not in TEXT_SUFFIXES:
                continue
            content = read_tracked(relative)
            if content is None:
                continue
            if home in content or (resolved != home and resolved in content):
                offenders.append(relative)
        self.assertEqual(offenders, [], f"these files contain the real home path: {offenders}")

    def test_no_tracked_file_contains_the_local_account_name(self):
        name = local_user_name()
        if not name:
            self.skipTest("account name is too generic to be identifying")
        pattern = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)
        offenders = [
            relative
            for relative in tracked_files()
            if Path(relative).suffix.lower() in TEXT_SUFFIXES
            and (content := read_tracked(relative)) is not None
            and pattern.search(content)
        ]
        self.assertEqual(offenders, [], f"these files contain the local account name: {offenders}")

    def test_no_runtime_state_is_tracked(self):
        offenders = [
            relative
            for relative in tracked_files()
            if any(Path(relative).match(pattern) for pattern in RUNTIME_PATTERNS)
        ]
        self.assertEqual(offenders, [], f"runtime state is tracked: {offenders}")

    def test_no_credential_shaped_strings(self):
        # ${{ github.token }} is GitHub's own built-in reference and is fine.
        allowed = ("${{ github.token }}",)
        pattern = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})")
        offenders = []
        for relative in tracked_files():
            if Path(relative).suffix.lower() not in TEXT_SUFFIXES:
                continue
            content = read_tracked(relative)
            if content and pattern.search(content) and not any(a in content for a in allowed):
                offenders.append(relative)
        self.assertEqual(offenders, [], f"possible credentials in: {offenders}")


@unittest.skipUnless(tracked_files(), "not a git checkout")
class HistoryPrivacyTests(unittest.TestCase):
    """A path that was committed and later removed is still published."""

    def test_no_personal_path_was_ever_committed(self):
        home = os.path.expanduser("~")
        if len(home) < 6 or home == "/":
            self.skipTest("no usable home directory to search for")
        try:
            result = subprocess.run(
                ["git", "log", "--all", "-p"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git history unavailable")
        if result.returncode != 0:
            self.skipTest("git history unavailable")
        added = [line for line in result.stdout.splitlines() if line.startswith("+") and home in line]
        self.assertEqual(added[:5], [], "the real home path appears in a committed diff")


class LocalOnlyGuaranteeTests(unittest.TestCase):
    """The product claims not to phone home; keep that checkable."""

    def test_no_outbound_request_in_the_shipped_page(self):
        from preview import build_html

        page = build_html()
        # KaTeX's bundled tokenizer uses the word fetch heavily: as a method
        # (`e.fetch()`) and as a method definition (`fetch(){...}`). A substring
        # search for "fetch(" flags all 28 of them, and a naive "not preceded by
        # a dot" search still matches the definition, because the character
        # before it is "}". So the pattern has to exclude member access, method
        # definitions, and property definitions as well.
        self.assertIsNone(
            re.search(r"(?<![.\w$}])fetch\s*\(\s*(?!\{)", page),
            "the page calls the global fetch",
        )
        for marker in ("XMLHttpRequest", "navigator.sendBeacon", "WebSocket", "EventSource"):
            self.assertNotIn(marker, page, f"the page contains {marker}")
        # Dynamic import can pull in a remote module.
        self.assertIsNone(
            re.search(r"(?<![.\w$}])import\s*\(\s*(?!\{)", page), "the page uses dynamic import"
        )
        # The only outbound path is an explicit click on a link, which goes
        # through the native bridge rather than the page.
        self.assertIn("open_external_url", page)

    def test_document_text_is_never_written_to_the_log(self):
        import preview

        with tempfile.TemporaryDirectory() as directory:
            log_directory = os.path.join(directory, "logs")
            preview.configure_logging(log_directory)
            self.addCleanup(preview.configure_logging, tempfile.mkdtemp())

            marker = "这一行是文档正文，只应该出现在文档里"
            document = Path(directory) / "note.md"
            document.write_text(marker + "\n", encoding="utf-8")

            class FakeWindow:
                def evaluate_js(self, script):
                    pass

                def set_title(self, title):
                    pass

            app = preview.PreviewApp(session_path=os.path.join(directory, "session.json"))
            app._window = FakeWindow()
            app.load_file(str(document))
            app._on_closing()

            with open(os.path.join(log_directory, "previewmd.log"), encoding="utf-8") as handle:
                self.assertNotIn(marker, handle.read())


if __name__ == "__main__":
    unittest.main()
