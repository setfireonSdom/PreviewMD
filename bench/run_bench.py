"""Headless WKWebView benchmark for PreviewMD.

Runs the real ``preview.build_html()`` page in an offscreen WKWebView (the same
WebKit engine the packaged app uses) and reports how long it takes to open a
document and to run an in-page search. This exists because V8/Node timings do
not predict JavaScriptCore: a single ``marked`` lexer pass over a 446 KB book
takes ~15 ms in Node but ~10 s in WebKit, which is why parsing is chunked.

Usage:
    .venv/bin/python bench/run_bench.py                    # all samples
    .venv/bin/python bench/run_bench.py plain_long         # one sample
    .venv/bin/python bench/run_bench.py --file path.md     # a real document

Requires macOS with PyObjC (installed with pywebview) and a graphical session.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from preview import build_html  # noqa: E402

WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
(async function () {
    var results = { customHighlightAPI: supportsCustomHighlight() };
    var content = document.getElementById("content");

    var t = performance.now();
    window.addTabFromPython("bench.md", null, window.__BENCH_DOC, null);
    results.firstPaintMs = Math.round(performance.now() - t);
    if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
    results.fullRenderMs = Math.round(performance.now() - t);
    results.paragraphs = content.querySelectorAll("p").length;
    results.codeBlocks = content.querySelectorAll("pre code").length;
    results.headings = content.querySelectorAll("h1,h2,h3,h4,h5,h6").length;

    var t2 = performance.now();
    var rerender = renderContent(tabs[activeIdx].content, tabs[activeIdx].path, true, tabs[activeIdx]);
    results.rerenderFirstBatchMs = Math.round(performance.now() - t2);
    await rerender;
    results.rerenderFullMs = Math.round(performance.now() - t2);

    results.katexEager = content.querySelectorAll(".katex").length;

    var doc = window.__BENCH_DOC;
    var frequency = {};
    for (var c = 0; c < doc.length; c += 1) {
        var ch = doc.charAt(c);
        if (ch.trim() === "") continue;
        frequency[ch] = (frequency[ch] || 0) + 1;
    }
    var queries = Object.keys(frequency)
        .sort(function (a, b) { return frequency[b] - frequency[a]; })
        .slice(0, 3);
    results.searchMs = [];
    for (var i = 0; i < queries.length; i++) {
        document.getElementById("search-bar").style.display = "block";
        document.getElementById("search-input").value = queries[i];
        var t3 = performance.now();
        doSearch();
        results.searchMs.push({ q: queries[i], ms: Math.round(performance.now() - t3), matches: searchMatches.length });
    }
    document.getElementById("search-bar").style.display = "none";
    doSearch();
    forceRenderAllMath();
    results.katexAfterForce = content.querySelectorAll(".katex").length;

    // Integration sanity checks for the paths that consume renderContent. The
    // editor round trip lays out a textarea holding the whole document, which is
    // impractically slow for multi-megabyte files, so it is only run on smaller
    // documents.
    results.exportStable = await awaitStableExportRender(tabs[activeIdx]);
    results.editorRoundTrip = "skipped";
    if (window.__BENCH_DOC.length <= 500000 || window.__BENCH_FORCE_EDITOR) {
        var t4 = performance.now();
        setViewMode("split");
        results.editorHasContent = document.getElementById("editor").value === tabs[activeIdx].content;
        document.getElementById("search-bar").style.display = "block";
        document.getElementById("search-input").value = queries[0];
        doSearch();
        results.editorSearchMatches = searchMatches.length;
        closeSearch();
        setViewMode("preview");
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        results.paragraphsAfterRoundTrip = content.querySelectorAll("p").length;
        results.roundTripPreserved = results.paragraphsAfterRoundTrip === results.paragraphs;
        results.editorRoundTrip = Math.round(performance.now() - t4);
    }

    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


def synthetic_samples() -> dict[str, str]:
    paragraph = (
        "一般年轻的读者，一看这本书是文言文，也许会以为难得读懂，不感兴趣。"
        "其实，只要你认真去读，不但可以完全读懂，而且会越读越觉得对自己大有益处。"
    )
    plain = "\n\n".join(paragraph for _ in range(4000))
    headings = "\n\n".join(f"## Section {i}\n\nBody text for section {i}." for i in range(5000))
    code = "\n\n".join(
        f"Intro {i}\n\n```python\ndef f_{i}(x):\n    return x + {i}\n```" for i in range(2000)
    )
    images = "\n\n".join(f"![figure {i}](assets/missing-{i}.png)\n\nCaption {i}." for i in range(500))
    math = "\n\n".join(f"Formula {i}: $a_{i} + b_{i} = c_{i}$ and more prose." for i in range(3000))
    tables = "\n\n".join(
        "| a | b | c |\n| - | - | - |\n" + "\n".join(f"| {i} | {i + 1} | {i + 2} |" for i in range(20))
        for _ in range(300)
    )
    return {
        "plain_long": plain,
        "many_headings": headings,
        "many_code_blocks": code,
        "many_images": images,
        "many_formulas": math,
        "many_tables": tables,
    }


def run_in_webview(html: str, script: str, timeout: float = 300.0) -> dict:
    import AppKit
    from Foundation import NSDate, NSRunLoop, NSURL
    from WebKit import WKWebView, WKWebViewConfiguration

    def pump(seconds: float) -> None:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))

    AppKit.NSApplicationLoad()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(1)

    config = WKWebViewConfiguration.alloc().init()
    webview = WKWebView.alloc().initWithFrame_configuration_(((0.0, 0.0), (1400.0, 900.0)), config)
    webview.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(PROJECT_ROOT) + "/"))

    deadline = time.time() + timeout

    def eval_js(expression: str):
        holder: dict = {"done": False, "value": None, "error": None}

        def handler(result, error):
            holder["value"] = result
            holder["error"] = str(error) if error else None
            holder["done"] = True

        webview.evaluateJavaScript_completionHandler_(expression, handler)
        while not holder["done"] and time.time() < deadline:
            pump(0.05)
        if not holder["done"]:
            raise TimeoutError("JavaScript evaluation did not return")
        if holder["error"]:
            raise RuntimeError(holder["error"])
        return holder["value"]

    while webview.isLoading() and time.time() < deadline:
        pump(0.05)
    pump(0.3)

    # The workload is async, so it publishes its result and we poll for it.
    eval_js(script)
    while time.time() < deadline:
        pump(0.05)
        if eval_js("window.__BENCH_DONE === true"):
            break
    else:
        raise TimeoutError("JavaScript workload did not finish")

    value = eval_js("window.__BENCH_RESULT")
    return json.loads(value) if isinstance(value, str) else value


def build_page(document: str, force_editor: bool = False) -> str:
    injection = "<script>window.__BENCH_DOC = " + json.dumps(document) + ";"
    injection += "window.__BENCH_FORCE_EDITOR = " + ("true" if force_editor else "false") + ";</script>\n</body>"
    return build_html().replace("</body>", injection, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", nargs="?", help="synthetic sample name")
    parser.add_argument("--file", help="measure a real Markdown file instead")
    parser.add_argument("--editor", action="store_true", help="force the editor round trip on large documents")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    if args.file:
        samples = {Path(args.file).name: Path(args.file).read_text(encoding="utf-8")}
    else:
        samples = synthetic_samples()
        if args.sample:
            if args.sample not in samples:
                parser.error(f"unknown sample {args.sample!r}; choose from {', '.join(samples)}")
            samples = {args.sample: samples[args.sample]}

    report = {}
    for name, document in samples.items():
        result = run_in_webview(build_page(document, args.editor), WORKLOAD)
        result["chars"] = len(document)
        report[name] = result
        if not args.json:
            searches = "  ".join(f"{s['q']}={s['ms']}ms({s['matches']})" for s in result["searchMs"])
            ok = ""
            if result.get("editorRoundTrip") != "skipped":
                ok = (
                    f"  ok(editor={result['editorHasContent']},"
                    f"edSearch={result['editorSearchMatches']},"
                    f"roundTrip={result['roundTripPreserved']})"
                )
            print(
                f"{name:16} {result['chars']:>8}c  "
                f"paint={result['firstPaintMs']:>4}ms  full={result['fullRenderMs']:>5}ms  "
                f"rerender={result['rerenderFirstBatchMs']:>4}/{result['rerenderFullMs']:>5}ms  "
                f"export={result['exportStable']}  editorRT={result['editorRoundTrip']}  "
                f"search[{searches}]{ok}"
            )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
