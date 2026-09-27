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


SHORTCUT_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function press(key, options) {
    document.dispatchEvent(new KeyboardEvent("keydown", Object.assign({ key: key, bubbles: true, cancelable: true }, options || {})));
}
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    window.addTabFromPython("first.md", null, "# First\n\ncontent", null);
    if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
    window.addTabFromPython("second.md", null, "# Second\n\ncontent", null);
    if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
    results.openedTabs = tabs.length;

    // Cmd+2 -> switch to the second tab
    press("2", { metaKey: true });
    await wait(60);
    results.afterCmd2 = { activeIndex: activeIdx, name: tabs[activeIdx] ? tabs[activeIdx].name : null };

    // Cmd+W -> close the active tab
    press("w", { metaKey: true });
    await wait(200);
    results.afterCmdW = { tabCount: tabs.length, closedName: "second.md" };

    // Cmd+Shift+T -> reopen it
    press("t", { metaKey: true, shiftKey: true });
    await wait(300);
    results.afterCmdShiftT = { tabCount: tabs.length, activeName: tabs[activeIdx] ? tabs[activeIdx].name : null };

    // A digit that has no tab must do nothing
    press("9", { metaKey: true });
    await wait(60);
    results.afterCmd9 = { tabCount: tabs.length, activeName: tabs[activeIdx] ? tabs[activeIdx].name : null };

    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


TOC_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
    window.addTabFromPython("novel.txt", null, window.__BENCH_DOC, null);
    if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
    await wait(120);

    results.source = tocSource;
    results.targetCount = tocTargets.length;
    results.buttonCount = document.querySelectorAll("#toc-list button").length;

    if (tocTargets.length) {
        results.first = tocTargets[0].label.slice(0, 28);
        results.middle = tocTargets[Math.floor(tocTargets.length / 2)].label.slice(0, 28);
        results.last = tocTargets[tocTargets.length - 1].label.slice(0, 28);

        // Chapter lines repeat in converted books; they must collapse by number.
        var seen = {};
        var duplicates = 0;
        var unparsed = 0;
        tocTargets.forEach(function(target) {
            var match = CHAPTER_PATTERN.exec(target.label);
            if (!match) { unparsed += 1; return; }
            var key = chapterKey(match[1]);
            if (seen[key]) duplicates += 1;
            seen[key] = true;
        });
        results.duplicateChapters = duplicates;
        results.nonChapterTargets = unparsed;

        // Open the outline, jump to the middle, and confirm the highlight follows.
        // updateActiveHeading is called directly: requestAnimationFrame does not
        // fire in an offscreen webview.
        document.body.classList.add("toc-open");
        var middle = tocTargets[Math.floor(tocTargets.length / 2)].element;
        var content = document.getElementById("content");
        content.scrollTop = middle.offsetTop - 20;
        updateActiveHeading();
        await wait(60);
        results.activeFollowsScroll = activeTocId === middle.id;
        results.activeLabel = (tocButtons.get(activeTocId) || {}).textContent;
        results.progressText = document.getElementById("toc-progress").textContent;
        results.progressVisible = !document.getElementById("toc-progress").hidden;

        // The sidebar must scroll the active entry into view.
        var sidebar = document.getElementById("toc-sidebar");
        var button = tocButtons.get(activeTocId);
        var sidebarRect = sidebar.getBoundingClientRect();
        var buttonRect = button.getBoundingClientRect();
        results.tocDebug = {
            sidebarScrollTop: sidebar.scrollTop,
            sidebarScrollHeight: sidebar.scrollHeight,
            sidebarClientHeight: sidebar.clientHeight,
            sidebarOverflowY: getComputedStyle(sidebar).overflowY,
            sidebarTop: Math.round(sidebarRect.top),
            sidebarBottom: Math.round(sidebarRect.bottom),
            buttonTop: Math.round(buttonRect.top),
            buttonBottom: Math.round(buttonRect.bottom),
        };
        results.tocFollowedIntoView =
            buttonRect.top >= sidebarRect.top - 2 && buttonRect.bottom <= sidebarRect.bottom + 2;
    }
    } catch (error) {
        results.error = [
            error && error.name,
            error && error.message,
            error && error.stack,
        ].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


POSITION_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
        var path = "/Users/tester/book.txt";
        window.addTabFromPython("book.txt", path, window.__BENCH_DOC, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(120);

        var content = document.getElementById("content");
        content.scrollTop = (content.scrollHeight - content.clientHeight) * 0.5;
        await wait(60);
        captureActiveScrolls();

        var state = sessionState();
        results.savedRatio = state.positions[path];
        results.savedKeys = Object.keys(state.positions).length;
        results.activeRatio = Math.round(tabs[0].previewRatio * 10000) / 10000;

        // Re-open the same document from scratch and restore the saved position.
        var keep = state.positions[path];
        tabs.splice(0, tabs.length);
        activeIdx = -1;
        content.replaceChildren();
        window.addTabFromPython("book.txt", path, window.__BENCH_DOC, null);
        window.finishSessionRestore({ active: 0, view_mode: "preview", positions: (function () {
            var out = {}; out[path] = keep; return out;
        })() });
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(200);

        var range = content.scrollHeight - content.clientHeight;
        results.restoredRatio = range > 0 ? Math.round((content.scrollTop / range) * 10000) / 10000 : null;
        results.restoredWithinTwoPercent =
            results.restoredRatio !== null && Math.abs(results.restoredRatio - keep) < 0.02;
    } catch (error) {
        results.error = [error && error.name, error && error.message, error && error.stack].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


SCROLL_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
        window.addTabFromPython("novel.txt", null, window.__BENCH_DOC, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(120);

        var content = document.getElementById("content");
        var range = content.scrollHeight - content.clientHeight;
        results.range = range;
        results.targets = tocTargets.length;

        // With the outline closed, scrolling must not do any outline work.
        var closedStart = performance.now();
        for (var i = 0; i < 200; i++) {
            content.scrollTop = (range * i) / 200;
            updateActiveHeading();
        }
        results.closedMs = Math.round((performance.now() - closedStart) * 100) / 100;

        // With the outline open, every frame locates the active chapter and
        // refreshes the progress line.
        document.body.classList.add("toc-open");
        updateActiveHeading();
        var openStart = performance.now();
        for (var j = 0; j < 200; j++) {
            content.scrollTop = (range * j) / 200;
            updateActiveHeading();
        }
        var openTotal = performance.now() - openStart;
        results.openMs = Math.round(openTotal * 100) / 100;
        results.perFrameMs = Math.round((openTotal / 200) * 1000) / 1000;

        // A linear scan over every chapter, for comparison with the binary search.
        var linearStart = performance.now();
        var contentTop = content.getBoundingClientRect().top;
        for (var k = 0; k < 200; k++) {
            var current = tocTargets[0].element;
            for (var n = 0; n < tocTargets.length; n++) {
                if (tocTargets[n].element.getBoundingClientRect().top - contentTop <= 48) {
                    current = tocTargets[n].element;
                } else {
                    break;
                }
            }
        }
        results.linearMs = Math.round((performance.now() - linearStart) * 100) / 100;

        // The same walk, locating the chapter only. This is what every scroll
        // frame costs in practice; the progress line is the rest.
        var binaryStart = performance.now();
        for (var m = 0; m < 200; m++) {
            content.scrollTop = (range * m) / 200;
            activeTocTarget();
        }
        results.binaryOnlyMs = Math.round((performance.now() - binaryStart) * 100) / 100;
        results.binaryOnlyPerFrameMs =
            Math.round((results.binaryOnlyMs / 200) * 1000) / 1000;

        // How often the progress line actually changes during a normal read:
        // one screen at a time, the way a reader moves.
        var screenSteps = 0;
        var changes = 0;
        var previous = document.getElementById("toc-progress").textContent;
        for (var s = 0; s < 200; s++) {
            content.scrollTop = (range * s) / 200;
            updateActiveHeading();
            var text = document.getElementById("toc-progress").textContent;
            if (text !== previous) changes += 1;
            previous = text;
            screenSteps += 1;
        }
        results.progressTextChanges = changes + " of " + screenSteps;
    } catch (error) {
        results.error = [error && error.name, error && error.message, error && error.stack].join(" | ");
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
    parser.add_argument(
        "--shortcuts", action="store_true", help="verify the tab keyboard shortcuts instead of timing"
    )
    parser.add_argument(
        "--toc", action="store_true", help="verify chapter detection and outline behaviour"
    )
    parser.add_argument(
        "--position", action="store_true", help="verify the reading position is saved and restored"
    )
    parser.add_argument(
        "--scroll", action="store_true", help="measure the per-frame cost of following the outline"
    )
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

    if args.shortcuts:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), SHORTCUT_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.toc:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), TOC_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.position:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), POSITION_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.scroll:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), SCROLL_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

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
