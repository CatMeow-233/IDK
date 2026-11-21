"""Edge cases of model-response JSON extraction and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanc.foundation.errors import ProviderError
from leanc.llm.parsing import (
    MAX_RESPONSE_CHARS,
    check_keys,
    expect_files,
    expect_str,
    extract_json_object,
    parse_code_response,
    parse_repair_response,
    parse_spec_draft_response,
    repair_proposal_from_obj,
    strip_code_fence,
)

RESP = Path(__file__).parent / "fixtures" / "llm" / "responses"


# ------------------------------------------------------------------ extract_json_object
def test_plain_object_with_whitespace_and_bom():
    assert extract_json_object('﻿  \n{"a": 1, "b": [1, 2]}\n') == {"a": 1, "b": [1, 2]}


def test_json_fence_with_leading_and_trailing_prose():
    text = 'Here you go:\n```json\n{"files": {"a.c": "x"}}\n```\nThanks!'
    assert extract_json_object(text) == {"files": {"a.c": "x"}}


def test_untagged_and_uppercase_tagged_fences():
    assert extract_json_object('```\n{"k": "v"}\n```') == {"k": "v"}
    assert extract_json_object('prose\n```JSON\n{"k": 2}\n```') == {"k": 2}


def test_braces_inside_strings_and_prose():
    obj = {"files": {"a.c": 'int f(void) { const char *s = "}{"; return s[0] == \'}\'; }\n'}}
    text = "The fix {see below} is: " + json.dumps(obj) + " -- done {really}."
    assert extract_json_object(text) == obj


def test_expect_keys_skips_example_objects_in_prose():
    text = 'For example {"x": 1} is not the answer. Answer: {"root_cause": "r", "rationale": "q", "files": {}}'
    assert extract_json_object(text)["x"] == 1  # without hints the first object wins
    got = extract_json_object(text, expect_keys=["root_cause"])
    assert got["root_cause"] == "r"


def test_expect_keys_falls_back_to_first_object():
    assert extract_json_object('{"a": 1}', expect_keys=["missing"]) == {"a": 1}


def test_nested_objects_are_not_returned_separately():
    text = 'noise {"outer": {"inner": {"deep": 1}}, "files": {"a.c": "{"}} tail'
    got = extract_json_object(text, expect_keys=["deep"])
    assert got == {"outer": {"inner": {"deep": 1}}, "files": {"a.c": "{"}}


def test_raw_newlines_in_strings_are_tolerated():
    text = (RESP / "spec_draft_raw_newlines.txt").read_text()
    obj = extract_json_object(text)
    assert obj["spec_yaml"].startswith("leanc_spec: 1\nspec_id: clamp")


def test_markdown_fence_inside_json_string_value():
    text = (RESP / "codegen_fence_in_string.md").read_text()
    obj = extract_json_object(text, expect_keys=["files"])
    assert "```c" in obj["files"]["src/safe_array.c"]
    assert "Example usage" in obj["notes"]


@pytest.mark.parametrize("text", ["", "   \n", "﻿"])
def test_empty_response_raises(text):
    with pytest.raises(ProviderError, match="empty"):
        extract_json_object(text)


def test_no_json_raises_with_excerpt():
    text = (RESP / "no_json.txt").read_text()
    with pytest.raises(ProviderError, match="no JSON object") as ei:
        extract_json_object(text)
    assert "I am sorry" in str(ei.value)


def test_array_and_truncated_and_non_text():
    with pytest.raises(ProviderError, match="array"):
        extract_json_object("[1, 2, 3]")
    with pytest.raises(ProviderError, match="no JSON object"):
        extract_json_object('```json\n{"files": {"a.c": "int x;"\n```')
    with pytest.raises(ProviderError, match="not text"):
        extract_json_object(None)  # type: ignore[arg-type]


def test_too_large_response_rejected():
    with pytest.raises(ProviderError, match="too large"):
        extract_json_object("{" + " " * MAX_RESPONSE_CHARS + "}")


def test_error_excerpt_is_redacted():
    secret = "sk-" + "A1b2C3d4" * 4
    with pytest.raises(ProviderError) as ei:
        extract_json_object(f"cannot comply, my key is {secret}")
    assert secret not in str(ei.value)
    assert "[REDACTED]" in str(ei.value)


# ------------------------------------------------------------------ validators
def test_check_keys_required_unknown_strict():
    assert check_keys({"a": 1, "z": 2}, required=["a"]) == ["z"]
    with pytest.raises(ProviderError, match="missing required"):
        check_keys({"b": 1}, required=["a"])
    with pytest.raises(ProviderError, match="unexpected key"):
        check_keys({"a": 1, "z": 2}, required=["a"], strict=True)
    with pytest.raises(ProviderError, match="expected a JSON object"):
        check_keys([1], required=["a"])  # type: ignore[arg-type]


def test_expect_str_variants():
    assert expect_str({"n": ["a", "b"]}, "n") == "a\nb"
    assert expect_str({"n": None}, "n", required=False, default="d") == "d"
    with pytest.raises(ProviderError, match="must be a string"):
        expect_str({"n": 3}, "n")
    with pytest.raises(ProviderError, match="must not be empty"):
        expect_str({"n": "  "}, "n", allow_empty=False)
    with pytest.raises(ProviderError, match="missing required string"):
        expect_str({}, "n")


def test_expect_files_normalises_and_keeps_bad_paths_visible():
    files = expect_files({"files": {"./src//a.c": "int a;", "../evil.c": "x", "/abs.c": "y"}})
    assert files == {"src/a.c": "int a;\n", "../evil.c": "x\n", "/abs.c": "y\n"}
    with pytest.raises(ProviderError, match="more than once"):
        expect_files({"files": {"src/a.c": "1", "./src/a.c": "2"}})
    with pytest.raises(ProviderError, match="must be a string"):
        expect_files({"files": {"src/a.c": 1}})
    with pytest.raises(ProviderError, match="empty file path"):
        expect_files({"files": {" ": "x"}})
    with pytest.raises(ProviderError, match="non-empty object"):
        expect_files({"files": {}})
    with pytest.raises(ProviderError, match="must be an object"):
        expect_files({"files": ["src/a.c"]})
    assert expect_files({}, required=False) is None
    # fixtures are returned byte-for-byte
    assert expect_files({"files": {"a.c": "```c\nx\n```"}}, clean=False) == {"a.c": "```c\nx\n```"}


def test_strip_code_fence():
    assert strip_code_fence("```c\nint x;\n```") == "int x;\n"
    assert strip_code_fence("~~~\nint x;\n~~~\n") == "int x;\n"
    assert strip_code_fence("int x; /* ``` */\n") == "int x; /* ``` */\n"


# ------------------------------------------------------------------ typed parsers
def test_parse_repair_from_prose_and_fence():
    prop = parse_repair_response((RESP / "repair_prose_fenced.md").read_text())
    assert prop.root_cause.startswith("idx > len")
    assert list(prop.files) == ["src/safe_array.c"]  # "./" normalised
    assert "idx >= len" in prop.files["src/safe_array.c"]
    assert '"}"' in prop.files["src/safe_array.c"]
    assert prop.patch is None and prop.raw_response.startswith("Sure!")


def test_parse_repair_patch_neither_and_both():
    p = parse_repair_response(json.dumps({"root_cause": "r", "rationale": "q", "patch": "```diff\n--- a/x\n+++ b/x\n```"}))
    assert p.patch == "--- a/x\n+++ b/x\n" and p.files is None
    empty = parse_repair_response('{"root_cause": "r", "rationale": "q", "files": {}, "patch": ""}')
    assert empty.is_empty and "neither" in empty.note
    with pytest.raises(ProviderError, match="not both"):
        parse_repair_response(json.dumps({"root_cause": "r", "rationale": "q", "files": {"a.c": "x"}, "patch": "d"}))
    with pytest.raises(ProviderError, match="unified diff string"):
        parse_repair_response(json.dumps({"root_cause": "r", "rationale": "q", "patch": ["d"]}))


def test_parse_repair_lenient_vs_strict():
    lenient = parse_repair_response('{"files": {"a.c": "int a;"}, "extra": 1}')
    assert lenient.root_cause == "" and lenient.files == {"a.c": "int a;\n"}
    with pytest.raises(ProviderError, match="missing required"):
        repair_proposal_from_obj({"files": {"a.c": "x"}}, strict=True)
    with pytest.raises(ProviderError, match="unexpected key"):
        repair_proposal_from_obj({"root_cause": "r", "rationale": "q", "files": {"a.c": "x"}, "extra": 1}, strict=True)
    with pytest.raises(ProviderError, match="non-empty 'files'"):
        repair_proposal_from_obj({"root_cause": "r", "rationale": "q"}, strict=True)


def test_parse_code_response_strips_fenced_values():
    out = parse_code_response((RESP / "codegen_fence_in_string.md").read_text())
    src = out.files["src/safe_array.c"]
    assert src.startswith("#include") and "```" not in src and src.endswith("}\n")
    assert "early returns" in out.notes
    with pytest.raises(ProviderError, match="files"):
        parse_code_response('{"notes": "forgot the files"}')
    unknown = parse_code_response('{"files": {"a.c": "x"}, "explanation": "?"}')
    assert "explanation" in unknown.notes


def test_parse_spec_draft_variants():
    d = parse_spec_draft_response((RESP / "spec_draft_raw_newlines.txt").read_text())
    assert yaml.safe_load(d.spec_yaml)["spec_id"] == "clamp"
    assert d.harness_c is None and d.tests_c is None
    as_obj = parse_spec_draft_response(json.dumps({"spec_yaml": {"spec_id": "x", "version": 1}, "notes": ["a", "b"]}))
    assert yaml.safe_load(as_obj.spec_yaml) == {"spec_id": "x", "version": 1} and as_obj.notes == "a\nb"
    fenced = parse_spec_draft_response(json.dumps({"spec_yaml": "```yaml\nspec_id: y\n```", "harness_c": "int main(void){return 0;}"}))
    assert fenced.spec_yaml == "spec_id: y\n" and fenced.harness_c.endswith("\n")
    with pytest.raises(ProviderError, match="spec_yaml"):
        parse_spec_draft_response('{"harness_c": "x"}')
    with pytest.raises(ProviderError, match="must not be empty"):
        parse_spec_draft_response('{"spec_yaml": " "}')
