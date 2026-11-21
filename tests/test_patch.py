from __future__ import annotations

import random
import re

import pytest

from leanc.foundation.errors import PatchError
from leanc.repair.patch import (
    apply_unified_diff,
    candidate_hash,
    changed_paths,
    diff_stats,
    make_unified_diff,
    parse_unified_diff,
    proposal_to_contents,
    split_lines,
)

SRC = """#include "safe_array.h"

int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
{
    if (arr == NULL || out == NULL) {
        return SAFE_ARRAY_ERR_NULL;
    }
    if (idx > len) {
        return SAFE_ARRAY_ERR_RANGE;
    }
    *out = arr[idx];
    return SAFE_ARRAY_OK;
}
"""
FIXED = SRC.replace("if (idx > len) {", "if (idx >= len) {")

FIX_PATCH = """--- a/src/safe_array.c
+++ b/src/safe_array.c
@@ -5,7 +5,7 @@ int safe_array_get(const int *arr, size_t len, size_t idx, int *out)
     if (arr == NULL || out == NULL) {
         return SAFE_ARRAY_ERR_NULL;
     }
-    if (idx > len) {
+    if (idx >= len) {
         return SAFE_ARRAY_ERR_RANGE;
     }
     *out = arr[idx];
"""


def reader(files: dict[str, str]):
    return files.get


def test_parse_basic_hunk_and_prefixes():
    (fp,) = parse_unified_diff(FIX_PATCH)
    assert fp.old_path == fp.new_path == "src/safe_array.c"
    assert not fp.is_new_file and not fp.is_deletion
    (h,) = fp.hunks
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (5, 7, 5, 7)
    assert h.section.startswith("int safe_array_get")
    assert [op for op, _ in h.lines].count("-") == 1
    assert h.old_lines()[3] == "    if (idx > len) {\n"
    assert h.new_lines()[3] == "    if (idx >= len) {\n"


def test_parse_omitted_counts_and_dev_null_and_timestamps():
    text = (
        "--- a/x.c\t2024-01-01 00:00:00.000000000 +0000\n"
        "+++ b/x.c\t2024-01-02 00:00:00.000000000 +0000\n"
        "@@ -2 +2 @@\n-b\n+B\n"
        "--- /dev/null\n+++ b/src/new.c\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    )
    a, b = parse_unified_diff(text)
    assert a.path == "x.c" and a.hunks[0].old_count == 1 and a.hunks[0].new_count == 1
    assert b.is_new_file and b.old_path is None and b.new_path == "src/new.c"
    assert b.hunks[0].new_lines() == ["one\n", "two\n"]


def test_parse_without_prefixes_and_normalises_paths():
    (fp,) = parse_unified_diff("--- ./src/x.c\n+++ ./src/x.c\n@@ -1 +1 @@\n-a\n+b\n")
    assert fp.path == "src/x.c"


def test_parse_no_newline_marker():
    text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    (fp,) = parse_unified_diff(text)
    assert fp.hunks[0].lines == [("-", "old"), ("+", "new")]


def test_parse_removed_line_that_looks_like_a_header():
    text = "--- a/f\n+++ b/f\n@@ -1,2 +1,1 @@\n--- x\n keep\n"
    (fp,) = parse_unified_diff(text)
    assert fp.hunks[0].lines == [("-", "-- x\n"), (" ", "keep\n")]
    assert apply_unified_diff(text, reader({"f": "-- x\nkeep\n"})) == {"f": "keep\n"}


def test_parse_tolerates_prose_and_code_fences():
    text = "Here is the fix:\n```diff\n" + FIX_PATCH + "```\nThanks.\n"
    assert apply_unified_diff(text, reader({"src/safe_array.c": SRC})) == {"src/safe_array.c": FIXED}


def test_parse_git_headers_and_empty_new_file():
    text = (
        "diff --git a/src/a.c b/src/a.c\nindex 1111111..2222222 100644\n"
        "--- a/src/a.c\n+++ b/src/a.c\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/src/empty.c b/src/empty.c\nnew file mode 100644\nindex 0000000..e69de29\n"
    )
    a, b = parse_unified_diff(text)
    assert a.path == "src/a.c" and not a.git_new_file
    assert b.is_new_file and b.path == "src/empty.c" and b.hunks == []
    assert apply_unified_diff(text, reader({"src/a.c": "a\n"})) == {"src/a.c": "b\n", "src/empty.c": ""}


@pytest.mark.parametrize(
    "text, msg",
    [
        ("", "no file patches"),
        ("just some prose\n", "no file patches"),
        ("--- a/f\n@@ -1 +1 @@\n-a\n+b\n", "not followed by a '+++'"),
        ("+++ b/f\n@@ -1 +1 @@\n-a\n+b\n", "without a preceding '---'"),
        ("@@ -1 +1 @@\n-a\n+b\n", "hunk header without"),
        ("--- a/f\n+++ b/f\n@@ -1,x +1 @@\n-a\n+b\n", "malformed hunk header"),
        ("--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n", "truncated"),
        ("--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-a\n+b\n+c\n", "unexpected line"),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n*b\n", "unexpected line in hunk"),
        ("--- a/f\n+++ b/f\n@@ -1,1 +1,2 @@\n a\n b\n", "more context lines"),
        ("--- /dev/null\n+++ /dev/null\n@@ -0,0 +1 @@\n+a\n", "both sides"),
        ("--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n", "unsafe path"),
        ("--- /etc/passwd\n+++ /etc/passwd\n@@ -1 +1 @@\n-a\n+b\n", "unsafe path"),
    ],
)
def test_parse_errors(text, msg):
    with pytest.raises(PatchError, match=re.escape(msg)):
        parse_unified_diff(text)


def test_parse_rejects_non_string():
    with pytest.raises(PatchError):
        parse_unified_diff(None)


def test_apply_simple_fix():
    assert apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": SRC})) == {"src/safe_array.c": FIXED}


def test_apply_with_offset_drift():
    three_lines_before_the_header_position = "/* a */\n/* b */\n/* c */\n"
    shifted = three_lines_before_the_header_position + SRC
    out = apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": shifted}))
    assert out["src/safe_array.c"] == three_lines_before_the_header_position + FIXED


def test_apply_offset_window_is_limited():
    far = "\n".join(f"/* {i} */" for i in range(80)) + "\n" + SRC
    with pytest.raises(PatchError, match="does not apply"):
        apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": far}), max_offset=10)
    assert apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": far}), max_offset=100)


def test_apply_mismatched_context_raises_clear_error():
    other = SRC.replace("*out = arr[idx];", "*out = arr[idx]; /* changed */")
    with pytest.raises(PatchError) as ei:
        apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": other}))
    msg = str(ei.value)
    assert "src/safe_array.c" in msg and "hunk #1" in msg and "expected" in msg and "found" in msg


def test_apply_is_fuzz_zero_whitespace_matters():
    tabs = SRC.replace("    if (idx > len) {", "\tif (idx > len) {")
    with pytest.raises(PatchError, match="does not apply"):
        apply_unified_diff(FIX_PATCH, reader({"src/safe_array.c": tabs}))


def test_apply_multiple_hunks_and_files():
    a = "".join(f"line {i}\n" for i in range(1, 31))
    b = "alpha\nbeta\n"
    new_a = a.replace("line 3\n", "LINE 3\n").replace("line 25\n", "line 25\nextra\n")
    new_b = "alpha\nBETA\ngamma\n"
    diff = make_unified_diff({"a.c": a, "b.c": b}, {"a.c": new_a, "b.c": new_b})
    fps = parse_unified_diff(diff)
    assert [f.path for f in fps] == ["a.c", "b.c"]
    assert len(fps[0].hunks) == 2
    assert apply_unified_diff(diff, reader({"a.c": a, "b.c": b})) == {"a.c": new_a, "b.c": new_b}


def test_apply_hunks_must_not_overlap_or_go_backwards():
    text = "--- a/f\n+++ b/f\n@@ -3 +3 @@\n-c\n+C\n@@ -1 +1 @@\n-a\n+A\n"
    with pytest.raises(PatchError, match="hunk #2"):
        apply_unified_diff(text, reader({"f": "a\nb\nc\n"}))


def test_apply_two_patches_for_same_file_compose():
    text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+A\n--- a/f\n+++ b/f\n@@ -2 +2 @@\n-b\n+B\n"
    assert apply_unified_diff(text, reader({"f": "a\nb\n"})) == {"f": "A\nB\n"}


def test_apply_new_file_and_existing_file_conflict():
    text = "--- /dev/null\n+++ b/src/new.c\n@@ -0,0 +1 @@\n+int x;\n"
    assert apply_unified_diff(text, reader({})) == {"src/new.c": "int x;\n"}
    with pytest.raises(PatchError, match="already exists"):
        apply_unified_diff(text, reader({"src/new.c": "int y;\n"}))


def test_apply_modifying_missing_file_fails():
    with pytest.raises(PatchError, match="does not exist"):
        apply_unified_diff(FIX_PATCH, reader({}))


def test_apply_rejects_delete_rename_mode_and_binary():
    with pytest.raises(PatchError, match="deleting"):
        apply_unified_diff("--- a/f\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n", reader({"f": "a\n"}))
    with pytest.raises(PatchError, match="deleting"):
        apply_unified_diff("diff --git a/f b/f\ndeleted file mode 100644\nindex 1..0\n", reader({"f": ""}))
    with pytest.raises(PatchError, match="renaming"):
        apply_unified_diff("--- a/f\n+++ b/g\n@@ -1 +1 @@\n-a\n+b\n", reader({"f": "a\n"}))
    with pytest.raises(PatchError, match="renaming"):
        apply_unified_diff(
            "diff --git a/f b/g\nsimilarity index 100%\nrename from f\nrename to g\n", reader({"f": "a\n"})
        )
    with pytest.raises(PatchError, match="mode"):
        apply_unified_diff("diff --git a/f b/f\nold mode 100644\nnew mode 100755\n", reader({"f": "a\n"}))
    with pytest.raises(PatchError, match="binary"):
        apply_unified_diff(
            "diff --git a/f.bin b/f.bin\nindex 1..2 100644\nBinary files a/f.bin and b/f.bin differ\n", reader({})
        )
    with pytest.raises(PatchError, match="binary"):
        apply_unified_diff("Binary files a/x.o and b/x.o differ\n", reader({}))


def test_apply_no_newline_handling():
    old = "a\nb"
    text = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b\n"
    assert apply_unified_diff(text, reader({"f": old})) == {"f": "a\nb\n"}
    context_needing_fuzz_without_the_marker = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
    with pytest.raises(PatchError):
        apply_unified_diff(context_needing_fuzz_without_the_marker, reader({"f": old}))


def test_no_newline_marker_in_the_middle_is_rejected():
    text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+A\n\\ No newline at end of file\n"
    with pytest.raises(PatchError, match="not be the last"):
        apply_unified_diff(text, reader({"f": "a\nb\n"}))


def test_empty_context_line_without_leading_space_is_accepted():
    old = "int f(void)\n{\n\n    return 1;\n}\n"
    text = "--- a/f.c\n+++ b/f.c\n@@ -2,4 +2,4 @@\n {\n\n-    return 1;\n+    return 2;\n }\n"
    assert apply_unified_diff(text, reader({"f.c": old})) == {"f.c": old.replace("1;", "2;")}


def test_crlf_files_round_trip():
    old = {"w.c": "int a;\r\nint b;\r\n"}
    new = {"w.c": "int a;\r\nint c;\r\n"}
    assert apply_unified_diff(make_unified_diff(old, new), reader(old)) == new


def _random_text(rng: random.Random, n: int) -> str:
    pool = ["{", "}", "", "return 0;", "x++;", "if (a) {", "int y = 1;", "/* c */", "\tz--;", "  "]
    lines = [rng.choice(pool) for _ in range(n)]
    text = "\n".join(lines)
    return text + ("\n" if rng.random() < 0.7 else "")


@pytest.mark.parametrize("seed", range(40))
def test_make_diff_apply_round_trip_random(seed):
    rng = random.Random(seed)
    old = {f"src/f{i}.c": _random_text(rng, rng.randint(0, 60)) for i in range(3)}
    new = dict(old)
    for p in list(new):
        lines = split_lines(new[p])
        for _ in range(rng.randint(0, 6)):
            op = rng.choice(["ins", "del", "mod"])
            k = rng.randint(0, len(lines))
            if op == "ins":
                lines.insert(k, rng.choice(["new line\n", "{\n", "}\n", "\n"]))
            elif lines and k < len(lines):
                if op == "del":
                    del lines[k]
                else:
                    lines[k] = "changed\n"
        new[p] = "".join(lines)
        if rng.random() < 0.2:
            new[p] = new[p].rstrip("\n")
    if rng.random() < 0.5:
        new["src/added.c"] = _random_text(rng, rng.randint(1, 10)) or "x\n"
    diff = make_unified_diff(old, new)
    touched = apply_unified_diff(diff, reader(old)) if diff else {}
    assert {**old, **touched} == new
    assert sorted(touched) == changed_paths(old, new)
    added, removed = diff_stats(old, new)
    body = [ln for ln in diff.splitlines() if not ln.startswith(("---", "+++", "@@", "\\"))]
    assert added == sum(1 for ln in body if ln.startswith("+"))
    assert removed == sum(1 for ln in body if ln.startswith("-"))


def test_make_unified_diff_format_is_deterministic():
    old = {"src/b.c": "x\n", "src/a.c": "1\n2\n3\n"}
    new = {"src/a.c": "1\nTWO\n3\n", "src/b.c": "x\n", "src/c.c": "new\n"}
    d1 = make_unified_diff(old, new)
    d2 = make_unified_diff(dict(reversed(list(old.items()))), dict(reversed(list(new.items()))))
    assert d1 == d2
    assert d1 == (
        "--- a/src/a.c\n+++ b/src/a.c\n@@ -1,3 +1,3 @@\n 1\n-2\n+TWO\n 3\n"
        "--- /dev/null\n+++ b/src/c.c\n@@ -0,0 +1 @@\n+new\n"
    )
    assert make_unified_diff(old, old) == ""


def test_make_unified_diff_empty_new_file_and_no_newline():
    diff = make_unified_diff({"a": "x"}, {"a": "x\n", "e.c": ""})
    assert "\\ No newline at end of file" in diff
    assert "--- /dev/null\n+++ b/e.c\n" in diff
    assert apply_unified_diff(diff, reader({"a": "x"})) == {"a": "x\n", "e.c": ""}


def test_make_unified_diff_handles_odd_path_names():
    old = {"src/my file.c": "a\n"}
    new = {"src/my file.c": "b\n", 'src/q"uote.c': "c\n"}
    diff = make_unified_diff(old, new)
    assert apply_unified_diff(diff, reader(old)) == new


def test_large_repetitive_file_diff_is_minimal_without_difflib_autojunk():
    body = "".join("    x = x + 1;\n}\n\n" for _ in range(200))
    old = {"f.c": body}
    new = {"f.c": body.replace("    x = x + 1;\n", "    x = x + 2;\n", 1)}
    assert diff_stats(old, new) == (1, 1)


def test_diff_stats_and_changed_paths():
    old = {"a": "1\n2\n3\n", "b": "same\n", "gone": "x\n"}
    new = {"a": "1\n3\n4\n5\n", "b": "same\n", "added": "n1\nn2\n"}
    assert changed_paths(old, new) == ["a", "added", "gone"]
    assert diff_stats(old, new) == (2 + 2, 1 + 1)
    assert diff_stats(old, old) == (0, 0)


def test_candidate_hash_is_canonical():
    a = {"src/a.c": "x", "src/b.c": "y"}
    b = {"src/b.c": "y", "src/a.c": "x"}
    assert candidate_hash(a) == candidate_hash(b)
    assert len(candidate_hash(a)) == 64
    assert candidate_hash(a) != candidate_hash({"src/a.c": "x", "src/b.c": "y "})
    assert candidate_hash({"ab": "c"}) != candidate_hash({"a": "bc"}), "path and content are not concatenated"
    assert candidate_hash({}) == candidate_hash({})


CURRENT = {"src/safe_array.c": SRC}


def test_proposal_full_files():
    out = proposal_to_contents({"src/safe_array.c": FIXED}, None, CURRENT)
    assert out == {"src/safe_array.c": FIXED}
    assert CURRENT == {"src/safe_array.c": SRC}, "the input is not mutated"


def test_proposal_patch():
    assert proposal_to_contents(None, FIX_PATCH, CURRENT) == {"src/safe_array.c": FIXED}
    assert proposal_to_contents({}, FIX_PATCH, CURRENT) == {"src/safe_array.c": FIXED}


@pytest.mark.parametrize("files, patch", [(None, None), ({}, ""), (None, "   \n"), ({}, None)])
def test_proposal_empty(files, patch):
    with pytest.raises(PatchError, match="empty proposal"):
        proposal_to_contents(files, patch, CURRENT)


def test_proposal_both_is_ambiguous():
    with pytest.raises(PatchError, match="ambiguous"):
        proposal_to_contents({"src/safe_array.c": FIXED}, FIX_PATCH, CURRENT)


def test_proposal_files_keys_normalised_or_kept_for_policy():
    out = proposal_to_contents({"./src/safe_array.c": FIXED}, None, CURRENT)
    assert out == {"src/safe_array.c": FIXED}
    kept_for_the_policy_to_name = proposal_to_contents({"/etc/passwd": "x", "harness/h.c": "y"}, None, CURRENT)
    assert kept_for_the_policy_to_name["/etc/passwd"] == "x" and kept_for_the_policy_to_name["harness/h.c"] == "y"
    with pytest.raises(PatchError, match="same file"):
        proposal_to_contents({"src/safe_array.c": "a", "src//safe_array.c": "b"}, None, CURRENT)
    with pytest.raises(PatchError):
        proposal_to_contents({"src/safe_array.c": 3}, None, CURRENT)


def test_proposal_patch_touching_protected_file_raises_with_clear_message():
    harness_edit = (
        "--- a/harness/safe_array_get_harness.c\n+++ b/harness/safe_array_get_harness.c\n"
        "@@ -1,1 +1,1 @@\n-/* Trusted CBMC harness for safe_array_get (human-reviewed). */\n+/* weakened */\n"
    )
    with pytest.raises(PatchError) as ei:
        proposal_to_contents(None, harness_edit, CURRENT)
    msg = str(ei.value)
    assert "harness/safe_array_get_harness.c" in msg and "not one of the editable sources" in msg


def test_proposal_patch_creating_file_yields_candidate_path():
    text = "--- /dev/null\n+++ b/src/extra.c\n@@ -0,0 +1 @@\n+int extra;\n"
    out = proposal_to_contents(None, text, CURRENT)
    assert out == {"src/safe_array.c": SRC, "src/extra.c": "int extra;\n"}


def test_proposal_patch_errors_are_patch_errors():
    with pytest.raises(PatchError):
        proposal_to_contents(None, "--- a/src/safe_array.c\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n", CURRENT)
    with pytest.raises(PatchError):
        proposal_to_contents(None, FIX_PATCH.replace("*out = arr[idx];", "*out = 0;"), CURRENT)
    with pytest.raises(PatchError):
        proposal_to_contents(None, 42, CURRENT)


def test_identical_proposal_produces_identical_candidate():
    assert proposal_to_contents({"src/safe_array.c": SRC}, None, CURRENT) == CURRENT
    assert changed_paths(CURRENT, proposal_to_contents({"src/safe_array.c": SRC}, None, CURRENT)) == []
