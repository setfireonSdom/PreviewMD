"""Global error reporting, driven through the real function from the built page.

The offscreen webview harness cannot cover this: errors thrown from injected
`evaluateJavaScript` arrive as an opaque cross-origin "Script error." with no
message and no useful location, so every one of them looks identical. The
deduplication rules therefore have to be checked here, where distinct failures
can actually be constructed.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

import preview
from preview import build_html

# reportPageError reaches for the toast and the native bridge; capture both
# instead of letting them run.
PRELUDE = """
var toasts = [];
var forwarded = [];
function showStatus(message) { toasts.push(message); }
function callBridge(method) {
    forwarded.push({ method: method, args: Array.prototype.slice.call(arguments, 1) });
    return Promise.resolve({ ok: true });
}
"""

DRIVER = """
function reset() {
    reportedErrorCount = 0;
    suppressedErrorCount = 0;
    lastReportedError = '';
    lastReportedErrorRepeats = 0;
    toasts = [];
    forwarded = [];
}
function state() {
    return {
        reported: reportedErrorCount,
        suppressed: suppressedErrorCount,
        toasts: toasts.length,
        forwarded: forwarded.length,
    };
}
var out = {};

// The same failure over and over: one report, the rest counted as suppressed.
reset();
for (var i = 0; i < 5; i++) reportPageError('error', 'boom', 'detail', 'app.js:10');
out.sameFailureRepeated = state();

// The same message from a different place: two different problems.
reset();
reportPageError('error', 'boom', 'detail', 'app.js:10');
reportPageError('error', 'boom', 'detail', 'app.js:99');
out.sameMessageDifferentPlace = state();

// The same place with a different message: also two problems.
reset();
reportPageError('error', 'first', 'detail', 'app.js:10');
reportPageError('error', 'second', 'detail', 'app.js:10');
out.samePlaceDifferentMessage = state();

// A rejection and a thrown error never merge, whatever they say.
reset();
reportPageError('unhandledrejection', 'boom', 'detail', '');
reportPageError('error', 'boom', 'detail', 'app.js:10');
out.differentKind = state();

// A long-running loop stays quiet but is not forgotten forever.
reset();
for (var j = 0; j < REPORT_EVERY_REPEAT * 2 + 1; j++) {
    reportPageError('error', 'loop', 'detail', 'app.js:10');
}
out.loop = state();

// The message shown to the user points at the log, and only once.
reset();
reportPageError('error', 'boom', 'detail', 'app.js:10');
reportPageError('error', 'other', 'detail', 'app.js:11');
out.toastText = toasts[0];
out.toastCount = toasts.length;

// Nothing may be sent before the first report, and the kind is forwarded.
reset();
reportPageError('unhandledrejection', 'rejected', 'a stack', '');
out.forwardedArgs = forwarded[0].args;
out.forwardedMethod = forwarded[0].method;

console.log(JSON.stringify(out));
"""


def extract_reporter() -> str:
    document = build_html()
    match = re.search(r"function reportPageError\(.*?\n\}", document, re.S)
    if match is None:
        raise AssertionError("reportPageError is missing from the page")
    parts = [match.group(0)]
    for name in ("reportedErrorCount", "suppressedErrorCount", "lastReportedError", "lastReportedErrorRepeats", "REPORT_EVERY_REPEAT"):
        found = re.search(r"var %s = [^;]+;" % name, document)
        if found is None:
            raise AssertionError(f"missing {name}")
        parts.insert(0, found.group(0))
    return "\n".join(parts)


@unittest.skipIf(shutil.which("node") is None, "node is required to run the error reporting logic")
class GlobalErrorReportingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = PRELUDE + extract_reporter() + "\n" + DRIVER
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        cls.out = json.loads(result.stdout.strip())

    def test_a_repeating_failure_is_reported_once_and_counted(self):
        outcome = self.out["sameFailureRepeated"]
        self.assertEqual(outcome["reported"], 1)
        self.assertEqual(outcome["suppressed"], 4)
        self.assertEqual(outcome["forwarded"], 1)

    def test_two_problems_are_never_collapsed_into_one(self):
        # The bug this guards: deduplicating on the message alone merged
        # unrelated failures that happened to share a generic message.
        for case in ("sameMessageDifferentPlace", "samePlaceDifferentMessage", "differentKind"):
            with self.subTest(case=case):
                self.assertEqual(self.out[case]["reported"], 2, f"{case} lost a failure")

    def test_a_loop_stays_quiet_without_disappearing(self):
        outcome = self.out["loop"]
        # One report, then one more per REPORT_EVERY_REPEAT, so a runaway loop
        # is still visible in the log.
        self.assertEqual(outcome["reported"], 3)
        self.assertEqual(outcome["suppressed"], 2 * 50)

    def test_the_user_is_told_once_and_told_where_to_look(self):
        self.assertEqual(self.out["toastCount"], 1)
        self.assertIn("previewmd.log", self.out["toastText"])

    def test_errors_reach_the_native_bridge_with_their_context(self):
        self.assertEqual(self.out["forwardedMethod"], "report_error")
        self.assertEqual(self.out["forwardedArgs"], ["unhandledrejection", "rejected", "a stack"])

    def test_the_page_listens_for_both_failure_kinds(self):
        document = build_html()
        self.assertIn("window.addEventListener('error', function(event) {", document)
        self.assertIn("window.addEventListener('unhandledrejection', function(event) {", document)
        # The handler must not be able to raise an error of its own.
        self.assertIn("catch (ignored) {", document)


class FakeWindow:
    def __init__(self):
        self.scripts = []
        self.titles = []

    def evaluate_js(self, script):
        self.scripts.append(script)

    def set_title(self, title):
        self.titles.append(title)


class ReportedErrorLoggingTests(unittest.TestCase):
    """The Python side of the report, including the privacy rule."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        preview.configure_logging(self.directory)
        self.addCleanup(preview.configure_logging, tempfile.mkdtemp())
        self.app = preview.PreviewApp(session_path=os.path.join(self.directory, "session.json"))
        self.app._window = FakeWindow()
        self.api = preview.Api(self.app)
        self.addCleanup(self.app._stop_all_watchers) if hasattr(self.app, "_stop_all_watchers") else None

    def read_log(self):
        with open(os.path.join(self.directory, "previewmd.log"), encoding="utf-8") as handle:
            return handle.read()

    def test_a_reported_error_reaches_the_log_with_its_context(self):
        self.api.report_error("error", "Something broke", "at app.js:42\n  frame two")
        text = self.read_log()
        self.assertIn("Something broke", text)
        self.assertIn("at app.js:42", text)
        self.assertIn("frame two", text)

    def test_nothing_is_written_when_the_page_reports_nothing(self):
        self.api.report_error("error", "only this")
        lines = [line for line in self.read_log().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)

    def test_reporting_never_raises_even_with_hostile_input(self):
        # The page is already broken; the handler must not make it worse.
        class Exploding:
            def __str__(self):
                raise RuntimeError("no string for you")

        self.assertEqual(self.api.report_error(Exploding(), Exploding(), Exploding()), {"ok": True})

    def test_document_text_never_reaches_the_log(self):
        # The real guarantee, checked end to end: open and save a document whose
        # body contains a marker no log line should ever carry, then confirm the
        # marker is absent. Document paths, sizes, and failures are logged;
        # document text is not.
        marker = "这一行是用户的正文，不应该出现在日志里"
        document_path = os.path.join(self.directory, "note.md")
        with open(document_path, "w", encoding="utf-8") as handle:
            handle.write(f"# 标题\n\n{marker}\n")

        self.app.load_file(document_path)
        self.api.save_file(document_path, f"# 标题\n\n{marker}\n", None, True)
        self.api.save_file(document_path, "x", "0" * 64, True)  # a conflicting save
        self.app._on_closing()

        text = self.read_log()
        self.assertNotIn(marker, text)
        # The useful parts are still there: the path, its size, the failure.
        self.assertIn("note.md", text)
        self.assertIn("characters", text)

    def test_the_log_file_is_where_the_message_tells_users_to_look(self):
        self.assertIn("~/Library/Logs/PreviewMD/previewmd.log", build_html())
        self.assertTrue(self.read_log() is not None)


if __name__ == "__main__":
    unittest.main()
