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
import statistics
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
    document.dispatchEvent(new KeyboardEvent("keydown", Object.assign(
        { key: key, bubbles: true, cancelable: true }, options || {})));
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

    // Open the outline the way a user does, before measuring it. While the
    // sidebar is closed, renders deliberately skip building it, so toggling is
    // what forces the build.
    toggleToc();
    await wait(60);

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

        // updateActiveHeading is called directly: requestAnimationFrame does not
        // fire in an offscreen webview.
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
        // Reported after the outline is opened below: while the sidebar is
        // closed, renders skip building it on purpose.
        results.targetsWhileClosed = tocTargets.length;

        // With the outline closed, scrolling must not do any outline work.
        var closedStart = performance.now();
        for (var i = 0; i < 200; i++) {
            content.scrollTop = (range * i) / 200;
            updateActiveHeading();
        }
        results.closedMs = Math.round((performance.now() - closedStart) * 100) / 100;

        // With the outline open, every frame locates the active chapter and
        // refreshes the progress line. Opening it goes through toggleToc().
        toggleToc();
        updateActiveHeading();
        results.targets = tocTargets.length;
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


ERROR_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
        var toast = document.getElementById("status-toast");
        // Independent listener: how many error events actually arrive, and what
        // do they say? Keeps this measurement separate from the app's own dedupe.
        window.__RAW = [];
        window.addEventListener("error", function(e) {
            window.__RAW.push("error:" + (e && e.message ? e.message : "?"));
        });
        window.addEventListener("unhandledrejection", function(e) {
            window.__RAW.push("rejection:" + (e && e.reason && e.reason.message ? e.reason.message : "?"));
        });

        // One throwing site, so a repeat really is the same failure repeating.
        window.__THROW_SITE = function (message) { throw new Error(message); };

        // An uncaught error must be reported once, on screen.
        setTimeout(function() { window.__THROW_SITE("bench uncaught failure"); }, 0);
        await wait(250);
        results.reportedAfterUncaught = reportedErrorCount;
        results.toastMentionsLog = toast.textContent.indexOf("previewmd.log") >= 0;
        results.toastText = toast.textContent.slice(0, 60);

        // The same error again must not repeat the notice.
        setTimeout(function() { window.__THROW_SITE("bench uncaught failure"); }, 0);
        await wait(250);
        results.reportedAfterRepeat = reportedErrorCount;
        results.suppressedAfterRepeat = suppressedErrorCount;

        // A different error is a different problem and must be reported.
        setTimeout(function() { window.__THROW_SITE("bench second failure"); }, 0);
        await wait(250);
        results.reportedAfterDifferent = reportedErrorCount;

        // Rejections have to be caught too, not just thrown exceptions.
        Promise.reject(new Error("bench unhandled rejection"));
        await wait(250);
        results.reportedAfterRejection = reportedErrorCount;

        // The interface must still work after all of that.
        results.rawEvents = window.__RAW;
        results.stillRenders = document.getElementById("content") !== null;
        showStatus("interface still alive");
        results.toastStillWorks = toast.textContent === "interface still alive";
    } catch (error) {
        results.error = [error && error.name, error && error.message].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


TOC_BUILD_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
function round2(value) {
    return Math.round(value * 100) / 100;
}
(async function () {
    var results = {};
    try {
        window.addTabFromPython("novel.txt", null, window.__BENCH_DOC, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(150);

        results.targets = tocTargets.length;
        results.tocOpenDuringTest = document.body.classList.contains("toc-open");

        // What any "skip the rebuild" strategy has to pay to find out whether
        // the outline actually changed: collect the targets, build nothing.
        var collectRuns = [];
        for (var c = 0; c < 3; c++) {
            var c0 = performance.now();
            collectTocTargets();
            collectRuns.push(performance.now() - c0);
        }
        results.collectOnlyMs = round2(collectRuns[1]);

        // One full rebuild, split into collection and everything else.
        var fullRuns = [];
        for (var f = 0; f < 3; f++) {
            var f0 = performance.now();
            rebuildToc();
            fullRuns.push(performance.now() - f0);
        }
        results.fullRebuildMs = round2(fullRuns[1]);
        results.domAndLayoutMs = round2(fullRuns[1] - collectRuns[1]);

        // Count what typing in split view actually triggers. The content
        // changes every time, so the "identical content" skip never applies.
        var calls = 0;
        var totalMs = 0;
        var original = rebuildToc;
        rebuildToc = function() {
            calls += 1;
            var started = performance.now();
            var result = original.apply(this, arguments);
            totalMs += performance.now() - started;
            return result;
        };
        for (var i = 1; i <= 3; i++) {
            renderContent(window.__BENCH_DOC + "\n\n\u7b2c" + i + " \u6b21\u6539\u52a8", null, true, tabs[0]);
            if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        }
        results.typingRenders = 3;
        results.rebuildsDuringTyping = calls;
        results.rebuildMsDuringTyping = round2(totalMs);

        // With the outline open, typing inside a chapter leaves the chapter list
        // unchanged, so the buttons should be kept rather than rebuilt.
        toggleToc();
        var openCalls = 0;
        var openMs = 0;
        var originalOpen = rebuildToc;
        rebuildToc = function() {
            openCalls += 1;
            var t3 = performance.now();
            var r = originalOpen.apply(this, arguments);
            openMs += performance.now() - t3;
            return r;
        };
        var buttonsBefore = document.querySelectorAll("#toc-list button").length;
        for (var j = 1; j <= 3; j++) {
            // 改动落在章节正文里，不新增也不删除章节
            renderContent(window.__BENCH_DOC + "\n\n\u6b63\u6587\u6539\u52a8 " + j, null, true, tabs[0]);
            if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        }
        results.openRebuildsDuringTyping = openCalls;
        results.openRebuildMsDuringTyping = round2(openMs);
        results.buttonsReused = document.querySelectorAll("#toc-list button").length === buttonsBefore;
        results.openTargets = tocTargets.length;
        results.openTargetAttached = document.contains(tocTargets[Math.floor(tocTargets.length / 2)].element);

        // Are the collected elements still attached after a re-render? This is
        // what makes "skip the rebuild" unsafe without rebuilding on open.
        var before = tocTargets[Math.floor(tocTargets.length / 2)].element;
        renderContent(window.__BENCH_DOC + "\n\n\u53e6\u4e00\u6b21", null, true, tabs[0]);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        results.targetStillAttached = document.contains(before);
    } catch (error) {
        results.error = [error && error.name, error && error.message, error && error.stack].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


DRAFT_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
        // Two drafts, one of them the active tab, typed into by hand.
        newUntitledTab();
        tabs[0].name = "notes.md";
        tabs[0].content = "# 草稿一\n\n还没保存的想法。";
        tabs[0].dirty = true;
        captureActiveScrolls();
        newUntitledTab();
        tabs[1].name = "outline.md";
        tabs[1].content = "# 草稿二\n\n第二份未保存内容。";
        tabs[1].dirty = true;
        captureActiveScrolls();

        var state = sessionState();
        results.draftCount = state.drafts.length;
        results.draftNames = state.drafts.map(function(d) { return d.name; });
        results.activeDraft = state.active_draft;
        results.fileTabs = state.tabs.length;

        // Simulate a restart: throw the tabs away and replay the session.
        var replay = JSON.parse(JSON.stringify(state));
        tabs.splice(0, tabs.length);
        activeIdx = -1;
        document.getElementById("content").replaceChildren();
        window.finishSessionRestore(replay);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(120);

        results.restoredCount = tabs.length;
        results.restoredNames = tabs.map(function(t) { return t.name; });
        results.restoredContent = tabs.map(function(t) { return t.content.slice(0, 12); });
        results.activeName = tabs[activeIdx] ? tabs[activeIdx].name : null;
        results.activeHasNoPath = tabs[activeIdx] ? !tabs[activeIdx].path : false;
        results.activeDirty = tabs[activeIdx] ? tabs[activeIdx].dirty : false;
        // This is the gate on the close confirmation: if it is false, quitting
        // discards the restored draft without asking.
        results.unsavedWorkReported = hasUnsavedWork();
        results.closeWouldWarn = hasUnsavedWork();
        results.renderedHeading = document.querySelector("#content h1") ?
            document.querySelector("#content h1").textContent : null;
    } catch (error) {
        results.error = [error && error.name, error && error.message, error && error.stack].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


FONT_SIZE_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) {
    return new Promise(function(resolve) { setTimeout(resolve, ms); });
}
(async function () {
    var results = {};
    try {
        window.addTabFromPython("a.md", null, "# 标题\n\n正文内容，用来测量字号。", null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(80);
        setViewMode("split");
        await wait(120);

        results.buttons = document.querySelectorAll("#settings-sizes button").length;
        var measured = {};
        ["small", "default", "large", "huge"].forEach(function(key) {
            setFontSize(key);
            var content = document.getElementById("content");
            var editor = document.getElementById("editor");
            measured[key] = {
                content: Math.round(parseFloat(getComputedStyle(content).fontSize)),
                editor: Math.round(parseFloat(getComputedStyle(editor).fontSize)),
                checked: document.querySelector('#settings-sizes button[aria-checked="true"]').dataset.sizeKey
            };
        });
        results.measured = measured;

        // Content must grow monotonically, and both panes must move together.
        var order = ["small", "default", "large", "huge"].map(function(k) { return measured[k].content; });
        results.contentAscending = order[0] < order[1] && order[1] < order[2] && order[2] < order[3];
        results.editorAscending = (function() {
            var e = ["small", "default", "large", "huge"].map(function(k) { return measured[k].editor; });
            return e[0] < e[1] && e[1] < e[2] && e[2] < e[3];
        })();
        results.checkmarksFollow = ["small", "default", "large", "huge"].every(function(k) {
            return measured[k].checked === k;
        });

        // The menu opens, and Escape closes it.
        document.getElementById("tab-settings").click();
        results.menuOpens = document.getElementById("settings-menu").classList.contains("open");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        results.menuClosesOnEscape = !document.getElementById("settings-menu").classList.contains("open");

        // The editor highlight mirror must not disagree with the editor.
        setFontSize("large");
        document.getElementById("search-input").style.display = "block";
        openSearch();
        document.getElementById("search-input").value = "正文";
        doSearch();
        var layer = document.getElementById("editor-highlights");
        results.mirrorFontSize = Math.round(parseFloat(getComputedStyle(layer).fontSize));
        results.editorFontSize = Math.round(parseFloat(getComputedStyle(document.getElementById("editor")).fontSize));
        results.mirrorMatchesEditor = results.mirrorFontSize === results.editorFontSize;
    } catch (error) {
        results.error = [error && error.name, error && error.message].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


MATH_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) { return new Promise(function(resolve) { setTimeout(resolve, ms); }); }
(async function () {
    var results = {};
    try {
        // Every spelling of a formula, plus the text that must stay literal.
        // A display formula spread over several lines used to arrive as $$,
        // <br>, formula, <br>, $$ and match nothing, so the page showed the
        // LaTeX source. Only the real engine can show that regression, because
        // the breaks behaviour is what breaks the formula apart.
        var cases = {
            inline: "前 $a^2+b^2$ 后",
            displayOneLine: "前\n\n$$a^2+b^2$$\n\n后",
            displayMultiLine: "前\n\n$$\na^2+b^2\n$$\n\n后",
            displayIndented: "前\n\n$$\n  \\frac{a}{b}\n$$\n\n后",
            inFence: "前\n\n```latex\n$$\na^2\n$$\n```\n\n后",
            inInlineCode: "前 `$$a^2$$` 后",
            escaped: "价格 \\$5 与 \\$10",
            currency: "预算 $1.75 万亿$ 没问题",
            unterminated: "前\n\n$$\na^2\n\n后"
        };
        var names = Object.keys(cases);
        results.cases = {};
        for (var i = 0; i < names.length; i++) {
            var name = names[i];
            tabs.splice(0, tabs.length);
            activeIdx = -1;
            document.getElementById("content").replaceChildren();
            window.addTabFromPython(name + ".md", null, cases[name], null);
            if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
            forceRenderAllMath();
            await wait(120);
            var content = document.getElementById("content");
            results.cases[name] = {
                katex: content.querySelectorAll(".katex").length,
                display: content.querySelectorAll(".katex-display").length,
                errors: content.querySelectorAll(".katex-error").length
            };
        }
        results.expect = {
            rendered: ["inline", "displayOneLine", "displayMultiLine", "displayIndented"],
            literal: ["inFence", "inInlineCode", "escaped", "currency", "unterminated"]
        };
        results.allRendered = results.expect.rendered.every(function(name) {
            return results.cases[name].katex > 0 && results.cases[name].errors === 0;
        });
        results.allLiteral = results.expect.literal.every(function(name) {
            return results.cases[name].katex === 0;
        });
        results.displayBlocks = results.expect.rendered.slice(1).every(function(name) {
            return results.cases[name].display === 1;
        });
    } catch (error) {
        results.error = [error && error.name, error && error.message].join(" | ");
    }
    window.__BENCH_RESULT = JSON.stringify(results);
    window.__BENCH_DONE = true;
})();
"started";
"""


WORD_COUNT_WORKLOAD = r"""
window.__BENCH_DONE = false;
window.__BENCH_RESULT = null;
function wait(ms) { return new Promise(function(resolve) { setTimeout(resolve, ms); }); }
function stats() {
    return {
        words: document.getElementById("status-words").textContent,
        characters: document.getElementById("status-characters").textContent
    };
}
function expected(text) {
    var counts = countDocumentText(text);
    return "字数 " + formatCount(counts.words) + " / 字符 " + formatCount(counts.characters);
}
function rect(id) {
    var r = document.getElementById(id).getBoundingClientRect();
    return { top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) };
}
(async function () {
    var results = {};
    try {
        results.emptyWindow = {
            display: getComputedStyle(document.getElementById("status-bar")).display,
            tabBar: rect("tab-bar")
        };

        var small = "# 标题\n\n短文档，五个汉字。\n";
        window.addTabFromPython("small.md", null, small, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(80);
        results.opened = { shown: stats(), expected: expected(small) };
        results.layout = {
            tabBar: rect("tab-bar"),
            mainArea: rect("main-area"),
            statusBar: rect("status-bar"),
            innerHeight: window.innerHeight
        };
        // The tab bar keeps its 36px and the statistics bar its own 24px.
        results.layoutKeepsBars = results.layout.tabBar.height === 36 &&
            results.layout.statusBar.height === 24 &&
            results.layout.mainArea.bottom === results.layout.statusBar.top;

        // Typing follows the keyboard on a small document.
        setViewMode("edit");
        await wait(80);
        var editor = document.getElementById("editor");
        editor.value = small + "再补六个字。";
        editor.dispatchEvent(new Event("input", { bubbles: true }));
        await wait(60);
        results.afterTyping = { shown: stats(), expected: expected(editor.value) };

        // Undo and redo go through the same path.
        editor.focus();
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "z", metaKey: true, bubbles: true }));
        await wait(120);
        results.afterUndo = { shown: stats(), expected: expected(editor.value) };
        document.dispatchEvent(new KeyboardEvent("keydown", {
            key: "z", metaKey: true, shiftKey: true, bubbles: true
        }));
        await wait(120);
        results.afterRedo = { shown: stats(), expected: expected(editor.value) };
        setViewMode("preview");
        await wait(60);

        // A second document gets its own numbers.
        window.addTabFromPython("second.md", null, "# Second\n\nanother document", null);
        await wait(120);
        results.secondTab = { shown: stats(), expected: expected("# Second\n\nanother document") };
        var edited = small + "再补六个字。";
        switchTab(0);
        await wait(150);
        results.backToFirst = { shown: stats(), expected: expected(edited) };

        // An external reload replaces the text.
        applyDiskSnapshot(tabs[activeIdx], "# 换了内容\n\n全部重写。", null);
        await wait(120);
        results.afterDiskReload = { shown: stats(), expected: expected("# 换了内容\n\n全部重写。") };

        // A novel-sized document is counted as soon as it opens: one pass over
        // millions of characters is cheaper than the render that follows it.
        var novel = "第三章 雨夜\n\n" + ("他抬头看了一眼窗外的雨，然后继续写下去。\n\n" * 40) * 60;
        var started = performance.now();
        window.addTabFromPython("novel.txt", null, novel, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        results.large = {
            shown: stats(),
            expected: expected(novel),
            settledInMs: Math.round(performance.now() - started)
        };

        // Typing in a document past the threshold waits for a pause instead of
        // recounting the whole thing on every keystroke. Kept to a size the
        // editor can still lay out, since a textarea holding millions of
        // characters is impractically slow.
        var medium = "他抬头看了一眼窗外的雨。\n\n".repeat(15000);
        window.addTabFromPython("medium.txt", null, medium, null);
        if (lastRenderState && lastRenderState.promise) await lastRenderState.promise;
        await wait(200);
        var overThreshold = medium.length > WORD_COUNT_FULL_SPEED_CHARS;
        setViewMode("edit");
        await wait(150);
        var editor = document.getElementById("editor");
        var before = stats();
        editor.value = medium + "\n\n多写一句。";
        editor.dispatchEvent(new Event("input", { bubbles: true }));
        var during = stats();
        await wait(WORD_COUNT_LARGE_DELAY_MS + 250);
        results.deferredTyping = {
            overThreshold: overThreshold,
            before: before,
            during: during,
            after: stats(),
            expected: expected(editor.value),
            waitedForPause: during.words === before.words
        };
        setViewMode("preview");
        await wait(60);

        // Closing every document takes the bar away with it. The tabs are marked
        // saved first: closing dirty work asks for confirmation, and a
        // headless webview has nobody to answer the dialog.
        tabs.forEach(function (tab) { tab.dirty = false; tab.savePromise = null; });
        while (tabs.length) closeTab(0);
        await wait(150);
        results.afterClosingAll = {
            display: getComputedStyle(document.getElementById("status-bar")).display,
            tabBar: getComputedStyle(document.getElementById("tab-bar")).display
        };
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
    from Foundation import NSURL, NSDate, NSRunLoop
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


# ── Repeated measurement ──
# There is no timing gate here, and the reason is measured rather than assumed.
# On this machine a single run of identical code varied by 2.4 to 2.8 times
# (first paint 50..118 ms, full render 285..800 ms), and the median of five runs
# still moved by up to 2.2 times between two independent batches. A threshold
# loose enough to tolerate that spread would also tolerate a real regression, and
# a tighter one would fail every day. A gate nobody can trust gets switched off.
#
# So this only reports. Comparing a change against the previous build is worth
# doing, but do it the way the outline fix was verified: run both versions
# alternately in one session, which cancels machine drift. Structural invariants
# (the layout-read count in test_outline_lookup_cost, undo depth in
# test_undo_history) are deterministic and belong in the test suite instead.
# The samples worth repeating a measurement on: the longest plain document, the
# heaviest KaTeX document, and the heading-heavy one.
REPEAT_SAMPLES = ("plain_long", "many_formulas", "many_headings")
REPEAT_METRICS = ("first_paint_ms", "full_render_ms")
REPEAT_DEFAULT_RUNS = 5


def run_repeats(args, samples):
    """Measure the interesting samples several times and report median and spread."""
    runs = args.repeat if args.repeat else REPEAT_DEFAULT_RUNS
    collected = {name: {"first_paint_ms": [], "full_render_ms": []} for name in REPEAT_SAMPLES}

    for _ in range(runs):
        for name in REPEAT_SAMPLES:
            result = run_in_webview(build_page(samples[name], args.editor), WORKLOAD)
            collected[name]["first_paint_ms"].append(result["firstPaintMs"])
            collected[name]["full_render_ms"].append(result["fullRenderMs"])

    print(f"{'sample':16} {'metric':16} {'runs':>14} {'median':>9} {'spread':>8}")
    for name, metrics in collected.items():
        for metric in REPEAT_METRICS:
            values = sorted(metrics[metric])
            median = statistics.median(values)
            spread = values[-1] / values[0] if values[0] else 0
            print(
                f"{name:16} {metric:16} "
                f"{values[0]:>6.0f}..{values[-1]:<7.0f} {median:>8.0f}ms {spread:>7.1f}x"
            )
    print(
        "\nCompare by running the previous build the same way and alternating the two.\n"
        "A single number from a single run means nothing on this machine."
    )
    return 0


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
    parser.add_argument(
        "--errors", action="store_true", help="verify global JavaScript error reporting"
    )
    parser.add_argument(
        "--toc-build", action="store_true", help="measure what building the outline costs"
    )
    parser.add_argument(
        "--drafts", action="store_true", help="verify unsaved drafts survive a restart"
    )
    parser.add_argument(
        "--font-size", action="store_true", help="verify the reading size control"
    )
    parser.add_argument(
        "--math", action="store_true", help="verify every spelling of a LaTeX formula"
    )
    parser.add_argument(
        "--word-count", action="store_true", help="verify the statistics bar at the bottom"
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=0,
        metavar="N",
        help="measure the key samples N times and report median and spread",
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

    if args.errors:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), ERROR_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.toc_build:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), TOC_BUILD_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.font_size:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), FONT_SIZE_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.math:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), MATH_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.word_count:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), WORD_COUNT_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.drafts:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), DRAFT_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.scroll:
        sample = samples[next(iter(samples))]
        report = run_in_webview(build_page(sample, args.editor), SCROLL_WORKLOAD)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.repeat:
        return run_repeats(args, samples)

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
