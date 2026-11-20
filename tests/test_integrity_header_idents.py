"""A #define of an identifier used by the protected interface (before the
protected #include) would silently change the approved prototype/contract for
every gate; the integrity rules must reject it."""

from __future__ import annotations

from pathlib import Path

from leanc.backends.integrity import source_violations
from leanc.specs.loader import load_spec

EX = Path(__file__).resolve().parent.parent / "examples/safe_array_get"


def _check(text: str):
    spec = load_spec(EX / "spec.yaml")
    headers = {h: (EX / h).read_text() for h in spec.sources.headers}
    return source_violations(spec, "src/safe_array.c", text, headers=headers)


def test_redefining_an_interface_type_is_rejected():
    good = (EX / "src/safe_array.c").read_text()
    evil = "#define size_t unsigned char\n" + good
    v = [x for x in _check(evil) if x.rule == "SYMBOL_OVERRIDE"]
    assert v and "protected interface" in v[0].message


def test_redefining_an_interface_macro_is_rejected():
    good = (EX / "src/safe_array.c").read_text()
    evil = good.replace('#include "safe_array.h"', '#include "safe_array.h"\n#undef SAFE_ARRAY_ERR_RANGE\n#define SAFE_ARRAY_ERR_RANGE 0')
    assert any(x.rule == "SYMBOL_OVERRIDE" for x in _check(evil))


def test_shipped_source_is_clean():
    assert _check((EX / "src/safe_array.c").read_text()) == []
