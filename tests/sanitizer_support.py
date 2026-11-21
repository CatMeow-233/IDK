from __future__ import annotations

import textwrap
from pathlib import Path

from leanc.specs.model import Spec

HEADER = "#ifndef F_H\n#define F_H\nint f(int n);\n#endif\n"

DRIVER = textwrap.dedent(
    """\
    #include <stdio.h>
    #include <stdlib.h>
    #include "f.h"

    int main(int argc, char **argv)
    {
        int n = argc > 1 ? atoi(argv[1]) : 0;
        int r = f(n);
        printf("r=%d\\n", r);
        return r == 42 ? 0 : 1;
    }
    """
)

GOOD_SRC = '#include "f.h"\n\nint f(int n)\n{\n    return n + 32;\n}\n'


def make_spec(cases: list[dict], **tests_kw) -> Spec:
    return Spec.model_validate(
        {
            "spec_id": "san-test",
            "version": 1,
            "title": "sanitizer backend test",
            "requirement": "f(10) == 42",
            "target": {"function": "f", "signature": "int f(int n)", "returns": {"type": "int"}},
            "sources": {"editable": ["src/f.c"], "headers": ["include/f.h"], "include_dirs": ["include"]},
            "tests": {"driver": "tests/driver.c", "cases": cases, **tests_kw},
            "verification": {"harness": "harness/f_harness.c"},
            "gates": ["compile", "sanitizer"],
        }
    )


def make_project(root: Path, src: str, driver: str = DRIVER) -> Path:
    for d in ("src", "include", "tests"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "include" / "f.h").write_text(HEADER)
    (root / "src" / "f.c").write_text(src)
    (root / "tests" / "driver.c").write_text(driver)
    return root
