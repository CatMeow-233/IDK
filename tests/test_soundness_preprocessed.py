from __future__ import annotations

import dataclasses

import pytest
from integrity_support import EXAMPLES, READ_LINE, SOURCE, by_rule, integrity_gate, shipped

from leanc.backends.integrity import NO_LINEMARKER
from leanc.foundation.status import Outcome
from leanc.rules.c_rules import c_source_violations
from leanc.rules.preprocessor import has_linemarker_for
from leanc.specs.loader import load_spec
from leanc.toolchain.context import BackendContext

PREPROCESS_FLAG = "-E"
LINEMARKER_START = "#"
TRIGRAPH_RULE = "TRIGRAPH"
TRIGRAPH_HIDDEN_LINE_READ = (
    "    size_t pos = idx;\n    if (len == 4) {\n        pos = idx + 1;\n        // keep pos in range ??/\n"
    "        pos = idx;\n    }\n    *out = arr[pos];"
)
TRIGRAPH_TEXTS = ["// end ??/\nx = 1;\n", 'const char *s = "??=";\n', "int a ??( 2 ??);\n", "/* ??! */\n"]
NOT_TRIGRAPH_TEXTS = ["int x = a ? b : c;\n", 'const char *s = "??";\n', 'const char *s = "?" "?/";\n']


def _without_linemarkers(original):
    def run(self, argv, **kwargs):
        proc = original(self, argv, **kwargs)
        if PREPROCESS_FLAG not in argv:
            return proc
        kept = [line for line in proc.stdout.splitlines() if not line.startswith(LINEMARKER_START)]
        return dataclasses.replace(proc, stdout="\n".join(kept))

    return run


def test_preprocessed_output_without_a_linemarker_for_the_file_is_an_error(
    example_copy, needs_clang, tmp_path, monkeypatch
):
    monkeypatch.setattr(BackendContext, "run", _without_linemarkers(BackendContext.run))
    result = integrity_gate(example_copy, tmp_path)
    assert result.outcome is Outcome.ERROR and NO_LINEMARKER in result.summary, result.summary
    assert "preprocessed" not in result.details["files"][0]["checks"]


@pytest.mark.parametrize(
    "output, marked",
    [
        ('# 1 "src/a.c"\nint x;\n', True),
        ('# 1 "./src/a.c" 1\nint x;\n', True),
        ('#line 1 "src/a.c"\nint x;\n', False),
        ('# 1 "src/b.c"\nint x;\n', False),
        ("int x;\n", False),
    ],
)
def test_linemarker_presence(output, marked):
    assert has_linemarker_for("src/a.c", output) is marked


@pytest.mark.parametrize("text", TRIGRAPH_TEXTS)
def test_trigraphs_are_rejected_anywhere(text):
    spec = load_spec(EXAMPLES / "safe_array_get" / "spec.yaml")
    assert [v.rule for v in c_source_violations(spec, SOURCE, text) if v.rule == TRIGRAPH_RULE] != []


@pytest.mark.parametrize("text", NOT_TRIGRAPH_TEXTS)
def test_question_marks_that_are_not_trigraphs_pass(text):
    spec = load_spec(EXAMPLES / "safe_array_get" / "spec.yaml")
    assert [v for v in c_source_violations(spec, SOURCE, text) if v.rule == TRIGRAPH_RULE] == []


def test_trigraph_that_hides_a_line_from_clang_fails_integrity(example_copy, needs_clang, tmp_path):
    result = integrity_gate(example_copy, tmp_path, shipped().replace(READ_LINE, TRIGRAPH_HIDDEN_LINE_READ))
    assert result.outcome is Outcome.FAIL, result.summary
    assert [d.location.line for d in by_rule(result, TRIGRAPH_RULE)] == [14]
