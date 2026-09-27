"""Exactness of the outline reuse check, driven through the real function.

`rebuildToc` keeps the existing buttons when the outline has not changed. That
is only safe if the check is exact: a false "unchanged" leaves a button pointing
at an id that no longer exists, and clicking it scrolls nowhere. A hash would
be cheaper but a collision reads as "unchanged", which is precisely the unsafe
direction, so these tests pin the exact comparison down.
"""

import json
import re
import shutil
import subprocess
import unittest

from preview import build_html

DRIVER = """
function makeTargets(labels) {
    return labels.map(function(label, index) {
        return { label: label, id: 'previewmd-heading-' + index, level: 2 };
    });
}
var out = {};

// The first comparison always fails: nothing has been collected yet.
tocTargets = [];
out.againstEmpty = tocTargetsMatch(makeTargets(['a']));

tocTargets = makeTargets(['第一', '第二', '第三']);
out.identical = tocTargetsMatch(makeTargets(['第一', '第二', '第三']));
out.extraEntry = tocTargetsMatch(makeTargets(['第一', '第二', '第三', '第四']));
out.fewerEntries = tocTargetsMatch(makeTargets(['第一', '第二']));
out.retitled = tocTargetsMatch(makeTargets(['第一', '改过', '第三']));
out.reordered = tocTargetsMatch(makeTargets(['第三', '第二', '第一']));

// Same labels, different ids: must not be treated as unchanged, because the
// buttons on screen point at the old ids.
tocTargets = makeTargets(['第一', '第二']);
var swapped = makeTargets(['第一', '第二']);
swapped[0].id = 'previewmd-heading-changed';
out.sameLabelsDifferentIds = tocTargetsMatch(swapped);

// Same length, different characters, so a length-only check would pass.
tocTargets = makeTargets(['aaa', 'bbb']);
out.sameLengthDifferentText = tocTargetsMatch(makeTargets(['aab', 'bbc']));

console.log(JSON.stringify(out));
"""


def extract_match() -> str:
    document = build_html()
    match = re.search(r"function tocTargetsMatch\(targets\) \{.*?\n\}", document, re.S)
    if match is None:
        raise AssertionError("tocTargetsMatch is missing from the page")
    return match.group(0)


@unittest.skipIf(shutil.which("node") is None, "node is required to run the reuse check")
class TocReuseCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = extract_match() + "\n" + DRIVER
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(f"node failed: {result.stderr[:2000]}")
        cls.out = json.loads(result.stdout.strip())

    def test_an_unchanged_outline_is_reused(self):
        self.assertIs(self.out["identical"], True)

    def test_nothing_is_reused_before_the_first_build(self):
        self.assertIs(self.out["againstEmpty"], False)

    def test_any_structural_change_forces_a_rebuild(self):
        for case in ("extraEntry", "fewerEntries", "retitled", "reordered"):
            with self.subTest(case=case):
                self.assertIs(
                    self.out[case], False, f"{case} was wrongly treated as unchanged"
                )

    def test_changed_ids_force_a_rebuild_even_with_identical_labels(self):
        # The buttons on screen address targets by id. Reusing them after an id
        # change is what would make a click scroll nowhere.
        self.assertIs(self.out["sameLabelsDifferentIds"], False)

    def test_a_length_only_check_would_not_be_enough(self):
        self.assertIs(self.out["sameLengthDifferentText"], False)


if __name__ == "__main__":
    unittest.main()
