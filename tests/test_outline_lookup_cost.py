"""The cost of finding the active outline entry, measured rather than asserted.

`activeTocTarget` runs on every scroll frame. A novel can have well over a
thousand chapters, so a linear scan would mean a thousand layout reads per
frame. A string assertion cannot tell the two apart: a linear scan can be
written with any indentation, and asserting on its text only proves that one
particular spelling is absent.

So the real function is run against instrumented stand-in targets that count
their own `getBoundingClientRect` calls, and the count is checked against the
bound a binary search implies.
"""

import json
import re
import shutil
import subprocess
import unittest

from preview import build_html

TARGET_COUNT = 2000

# A stand-in for the scroll container plus TARGET_COUNT targets whose offsets
# increase with index, and which record how often their rect is read.
PRELUDE = """
var rectReads = 0;
var document = {
    getElementById: function(id) {
        if (id !== 'content') return null;
        return { getBoundingClientRect: function() { return { top: 0 }; } };
    }
};
"""

DRIVER = """
function makeTarget(index, top) {
    return {
        id: 'previewmd-heading-chapter-' + index,
        element: {
            getBoundingClientRect: function() {
                rectReads += 1;
                return { top: top };
            }
        }
    };
}

function measure(targetCount, scrollTop) {
    rectReads = 0;
    tocTargets = [];
    for (var i = 0; i < targetCount; i++) {
        // 100px apart, so a target is "current" once it is above the fold line.
        tocTargets.push(makeTarget(i, i * 100 - scrollTop));
    }
    var found = activeTocTarget();
    return { reads: rectReads, id: found ? found.id : null };
}

var out = { readsPerTargetCount: {} };
[10, 100, 1000, 2000].forEach(function (size) {
    // Scroll to the middle so the search cannot exit after one comparison.
    out.readsPerTargetCount[size] = measure(size, size * 50).reads;
});
out.foundAtTop = measure(2000, 0).id;
out.foundInMiddle = measure(2000, 100000).id;
out.foundAtEnd = measure(2000, 199900).id;
console.log(JSON.stringify(out));
"""


def extract_lookup() -> str:
    document = build_html()
    match = re.search(r"function activeTocTarget\(\) \{.*?\n\}", document, re.S)
    if match is None:
        raise AssertionError("activeTocTarget is missing from the page")
    return match.group(0)


@unittest.skipIf(shutil.which("node") is None, "node is required to measure the outline lookup")
class OutlineLookupCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = PRELUDE + extract_lookup() + "\n" + DRIVER
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        raw = json.loads(result.stdout.strip())
        # JSON object keys arrive as strings; index by chapter count instead.
        cls.out = dict(raw, readsPerTargetCount={int(k): v for k, v in raw["readsPerTargetCount"].items()})

    def test_layout_reads_stay_logarithmic_in_the_number_of_chapters(self):
        # Doubling the chapter count must not double the work. A linear scan
        # would read one rect per target; a binary search reads about
        # log2(targets), which is 11 steps for 2000 chapters.
        reads = self.out["readsPerTargetCount"]
        self.assertLessEqual(reads[2000], 20, f"2000 chapters cost {reads[2000]} layout reads")
        self.assertLessEqual(reads[1000], 20)
        # 1000 and 2000 chapters must cost the same order of magnitude.
        self.assertLess(reads[2000], reads[100] * 2)
        self.assertLess(reads[100], reads[10] * 3)

    def test_the_correct_chapter_is_found_at_any_position(self):
        self.assertEqual(self.out["foundAtTop"], "previewmd-heading-chapter-0")
        self.assertEqual(self.out["foundInMiddle"], "previewmd-heading-chapter-1000")
        self.assertEqual(self.out["foundAtEnd"], "previewmd-heading-chapter-1999")


if __name__ == "__main__":
    unittest.main()
