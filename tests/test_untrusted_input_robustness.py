from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from leanc.backends.lean.vectors import _is_vector_count
from leanc.backends.sanitizer.runtime_reports import parse_sanitizer_findings
from leanc.cli import main
from leanc.foundation.errors import ConfigError, ProviderError, SpecError
from leanc.foundation.text import safe_name, utf8_safe
from leanc.llm.openai_compat import OpenAICompatProvider
from leanc.llm.parsing import (
    extract_json_object,
    parse_code_response,
    parse_repair_response,
    parse_spec_draft_response,
)
from leanc.pipeline.config import load_config
from leanc.pipeline.generation import _untrusted_draft_data
from leanc.specs.approval import _read_store, approvals_dir
from leanc.specs.loader import parse_spec_text

REPO = Path(__file__).resolve().parent.parent
BASE_SPEC = (REPO / "examples/safe_array_get/spec.yaml").read_text()
LONE_SURROGATE = "\ud83d"
OVERLONG_DIGITS = "9" * 4301
NAME_MAX_BYTES = 255


def _spec_json(mutate) -> str:
    data = yaml.safe_load(BASE_SPEC)
    mutate(data)
    return json.dumps(data)


def _set_flags(data):
    data["compile"]["flags"] = ["-Wall\x00x"]


def _set_defines(data):
    data["compile"]["defines"] = ["X=1\x00"]


def _set_case_args(data):
    data["tests"]["cases"][0]["args"] = ["a\x00b"]


def _set_extra_checkers(data):
    data["static_analysis"]["extra_checkers"] = ["alpha\x00x"]


def _set_surrogate_title(data):
    data["title"] = "t" + LONE_SURROGATE


@pytest.mark.parametrize("mutate", [_set_flags, _set_defines, _set_case_args, _set_extra_checkers, _set_surrogate_title])
def test_spec_rejects_text_that_cannot_reach_a_tool(mutate):
    with pytest.raises(SpecError, match="text not allowed in a spec"):
        parse_spec_text(_spec_json(mutate), source="spec.json")


def test_spec_allows_nul_in_test_stdin_and_expected_stdout():
    def mutate(data):
        data["tests"]["cases"][0]["stdin"] = "a\x00b"
        data["tests"]["cases"][0]["expect_stdout"] = "a\x00b"

    spec = parse_spec_text(_spec_json(mutate), source="spec.json")
    assert spec.tests.cases[0].stdin == "a\x00b"


@pytest.mark.parametrize(
    "text, source",
    [
        ("spec_id: 2020-13-45\n", "spec.yaml"),
        ("a: !!bool x\n", "spec.yaml"),
        ("a: !!timestamp x\n", "spec.yaml"),
        ("a: !!float x\n", "spec.yaml"),
        ("[" * 5000, "spec.yaml"),
        ("? [1]\n: 2\n", "spec.yaml"),
        ("{[1]: 2}\n", "spec.yaml"),
        ("[" * 100000, "spec.json"),
        ("spec_id: !!int ''\n", "spec.yaml"),
        ("spec_id: !!float\n", "spec.yaml"),
        ("spec_id: " + ":".join(["59"] * 200) + ".0\n", "spec.yaml"),
    ],
)
def test_malformed_spec_documents_are_spec_errors(text, source):
    with pytest.raises(SpecError, match="cannot parse"):
        parse_spec_text(text, source=source)


def test_malformed_spec_document_exits_with_usage_error(tmp_path, capsys):
    spec = tmp_path / "spec.yaml"
    spec.write_text("spec_id: 2020-13-45\n")
    assert main(["spec", "show", str(spec)]) == 2
    assert "cannot parse" in capsys.readouterr().err


def test_malformed_project_config_is_config_error(tmp_path):
    (tmp_path / "leanc.yaml").write_text("provider: !!bool x\n")
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(tmp_path)


def test_non_utf8_approval_record_reads_as_corrupt(tmp_path):
    store = approvals_dir(tmp_path)
    store.mkdir(parents=True)
    (store / "s1.json").write_bytes(b"\xff\xfe{}")
    record, problem = _read_store(tmp_path, "s1")
    assert record is None and problem.startswith("corrupt approval record")


def test_deeply_nested_approval_record_reads_as_corrupt(tmp_path):
    store = approvals_dir(tmp_path)
    store.mkdir(parents=True)
    (store / "s1.json").write_text("[" * 100000)
    record, problem = _read_store(tmp_path, "s1")
    assert record is None and problem.startswith("corrupt approval record")


def test_llm_file_content_with_lone_surrogate_is_provider_error():
    response = json.dumps({"root_cause": "x", "files": {"src/a.c": "int x;" + LONE_SURROGATE}})
    with pytest.raises(ProviderError, match="lone UTF-16 surrogate"):
        parse_repair_response(response)
    with pytest.raises(ProviderError, match="lone UTF-16 surrogate"):
        parse_code_response(json.dumps({"files": {"src/a.c": LONE_SURROGATE}}))


def test_llm_spec_yaml_with_escaped_lone_surrogate_is_provider_error():
    with pytest.raises(ProviderError, match="lone UTF-16 surrogate"):
        _untrusted_draft_data('title: "\\ud83d"\n', "r", "s1", "mock")


def test_unparsable_llm_spec_yaml_is_provider_error():
    with pytest.raises(ProviderError, match="unparsable spec YAML"):
        _untrusted_draft_data("a: !!timestamp x\n", "r", "s1", "mock")


@pytest.mark.parametrize("text", ["[" * 100000, '{"a": ' + "[" * 100000])
def test_deeply_nested_llm_response_is_provider_error(text):
    with pytest.raises(ProviderError):
        extract_json_object(text)


def test_llm_transcript_with_lone_surrogate_is_written(tmp_path):
    provider = OpenAICompatProvider("http://127.0.0.1:9/v1", None, "m", transcript_dir=tmp_path)
    provider._write_transcript("chat", {"error": "quota " + LONE_SURROGATE})
    text = provider.last_transcript.read_text(encoding="utf-8")
    assert '"quota \\ud83d"' in text
    json.loads(text)


def test_aborted_run_with_surrogate_message_is_recorded_as_usage_error(tmp_path, capsys):
    rc = main(
        [
            "spec", "draft", "--id", "x", "--requirement", "r", "--provider", "mock",
            "--project-root", str(tmp_path), "--mock-dir", "/nonexistent-\udcff",
        ]
    )
    assert rc == 2
    assert "no spec draft fixture" in capsys.readouterr().err
    records = list((tmp_path / ".leanc" / "runs").glob("*/run.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text(encoding="utf-8"))["status"] == "TOOL_ERROR"


def test_utf8_safe_escapes_only_lone_surrogates():
    assert utf8_safe("ok \U0001f600 " + LONE_SURROGATE) == "ok \U0001f600 \\ud83d"


def test_safe_name_is_ascii_and_bounded():
    name = safe_name("src/" + "\U0001d400" * 200 + ".c")
    assert name.isascii() and len(name.encode("utf-8")) <= 80


@pytest.mark.parametrize(
    "stderr",
    [
        f"x.c:{OVERLONG_DIGITS}:1: runtime error: y\n",
        f"    #{OVERLONG_DIGITS} 0x1 in f x.c:1:1\n",
        f"    #0 0x1 in f x.c:{OVERLONG_DIGITS}:1\n",
        f"Direct leak of {OVERLONG_DIGITS} byte(s) in 1 object(s) allocated from:\n    #0 0x1 in malloc\n",
        f"SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior x.c:{OVERLONG_DIGITS}:1 in f\n",
    ],
)
def test_sanitizer_parser_ignores_overlong_numbers_from_untrusted_output(stderr):
    parse_sanitizer_findings(stderr)


def test_sanitizer_parser_still_reads_genuine_locations():
    findings = parse_sanitizer_findings("x.c:12:5: runtime error: division by zero\n")
    location = findings[0].diagnostic.location
    assert location.line == 12 and location.column == 5


@pytest.mark.parametrize("token, valid", [("12", True), ("\u00b2", False), ("\u2460", False), (OVERLONG_DIGITS, False)])
def test_lean_vector_count_token(token, valid):
    assert _is_vector_count(token) is valid


def test_overlong_editable_path_keeps_integrity_gate_recorded(example_copy, capsys, needs_clang):
    project = example_copy("safe_array_get")
    deep_dir = Path("src", *(["d" * 40] * 6))
    (project / deep_dir).mkdir(parents=True)
    (project / "src" / "safe_array.c").rename(project / deep_dir / "safe_array.c")
    deep_rel = (deep_dir / "safe_array.c").as_posix()
    assert len(f"preprocess-{deep_rel.replace('/', '_')}.txt".encode()) > NAME_MAX_BYTES
    spec = project / "spec.yaml"
    spec.write_text(spec.read_text().replace("editable: [src/safe_array.c]", f"editable: [{deep_rel}]"))
    assert main(["spec", "approve", str(spec), "--by", "t", "--yes"]) == 0
    capsys.readouterr()
    rc = main(["verify", str(spec), "--gates", "compile", "--json"])
    data = json.loads(capsys.readouterr().out)
    stages = {s["stage"]: s["outcome"] for s in data["verification"]["stages"]}
    assert stages.get("integrity") == "PASS", stages
    assert rc == 20 and data["status"] == "UNKNOWN"


DEEP = 1000


def test_deeply_nested_llm_object_is_provider_error():
    deep = "[" * DEEP + "]" * DEEP
    with pytest.raises(ProviderError, match="nested deeper"):
        parse_code_response('{"files": {"a.c": "int x;"}, "extra": ' + deep + "}")
    with pytest.raises(ProviderError, match="nested deeper"):
        parse_repair_response('{"root_cause": "x", "files": {"a.c": "int x;"}, "extra": ' + deep + "}")


def test_deeply_nested_llm_spec_mapping_is_provider_error():
    nested = '{"a": ' * 500 + "1" + "}" * 500
    with pytest.raises(ProviderError, match="nested deeper"):
        parse_spec_draft_response('{"spec_yaml": ' + nested + "}")


def test_deeply_nested_llm_spec_yaml_is_provider_error():
    with pytest.raises(ProviderError, match="nested deeper"):
        _untrusted_draft_data("title: t\nextra: " + "[" * 350 + "]" * 350 + "\n", "r", "s1", "mock")


@pytest.mark.parametrize("text", ["provider: !!int ''\n", "provider: !!float\n", "provider: " + ":".join(["59"] * 200) + ".0\n"])
def test_project_config_scalar_overflows_are_config_errors(tmp_path, text):
    (tmp_path / "leanc.yaml").write_text(text)
    with pytest.raises(ConfigError, match="cannot parse"):
        load_config(tmp_path)


def test_outgoing_llm_messages_escape_lone_surrogates():
    provider = OpenAICompatProvider("http://127.0.0.1:9/v1", None, "m")
    messages = provider._messages_without_own_key([{"role": "user", "content": "sort \udcff"}])
    assert json.dumps(messages, ensure_ascii=False).encode("utf-8")
    assert messages[0]["content"] == "sort \\udcff"


def test_llm_content_with_lone_surrogate_is_escaped_before_parsing():
    provider = OpenAICompatProvider("http://127.0.0.1:9/v1", None, "m")
    body = json.dumps({"choices": [{"message": {"content": "prose \ud83d {}"}, "finish_reason": "stop"}]})
    content, _ = provider._extract_content(body)
    assert content == "prose \\ud83d {}"


def test_approver_with_raw_non_utf8_bytes_is_usage_error(example_copy, capsys):
    spec = example_copy("safe_array_get") / "spec.yaml"
    assert main(["spec", "approve", str(spec), "--by", "alice\udcff", "--yes"]) == 2
    assert "valid Unicode" in capsys.readouterr().err


def test_overlong_path_component_is_spec_error():
    def mutate(data):
        data["sources"]["editable"] = ["src/" + "a" * 251 + ".c"]

    with pytest.raises(SpecError, match="path component longer than"):
        parse_spec_text(_spec_json(mutate), source="spec.json")


def test_spec_under_non_utf8_directory_name_is_usage_error_on_approve(example_copy, capsys):
    project = example_copy("safe_array_get")
    (project / "leanc.yaml").write_text("provider: mock\n")
    nested = project / "d\udcff"
    try:
        nested.mkdir()
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 directory names")
    (project / "spec.yaml").rename(nested / "spec.yaml")
    rc = main(["spec", "approve", str(nested / "spec.yaml"), "--project-root", str(project), "--by", "alice", "--yes"])
    assert rc == 2
    assert "valid Unicode" in capsys.readouterr().err


def test_spec_draft_with_non_utf8_requirement_and_title_does_not_crash(tmp_path, capsys):
    rc = main(
        [
            "spec", "draft", "--id", "x", "--requirement", "r\udcff", "--title", "t\udcff", "--provider", "mock",
            "--project-root", str(tmp_path), "--mock-dir", str(REPO / "examples/safe_array_get/mock"),
        ]
    )
    assert rc in (2, 40)
    assert "lone UTF-16 surrogate" in capsys.readouterr().out + (tmp_path / "drafts/x/NOTES.md").read_text()
