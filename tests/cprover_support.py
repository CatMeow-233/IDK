from __future__ import annotations

import dataclasses

from leanc.toolchain.environment import Toolchain

MEASURED_CBMC_VERSION = "5.95.1 (cbmc-5.95.1)"
MEASURED_GOTO_INSTRUMENT_VERSION = "5.95.1 (cbmc-5.95.1)"
MEASURED_GOTO_CC_VERSION = "gcc (goto-cc 5.95.1 (cbmc-5.95.1)) 13.3.0"
MEASURED_GOTO_LD_VERSION = "* *           goto-cc 5.95.1 (cbmc-5.95.1) 64-bit           * *"
MEASURED_GOTO_AS_VERSION = "GNU assembler version 2.20.51.0.7 20100318 (goto-cc 5.95.1 (cbmc-5.95.1))"
MEASURED_GOTO_GCC_VERSION = "gcc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0"
CBMC_PATH = "/opt/cprover/bin/cbmc"
GOTO_CC_PATH = "/opt/cprover/bin/goto-cc"
GOTO_INSTRUMENT_PATH = "/opt/cprover/bin/goto-instrument"
TOOL_FIELDS = {
    "cbmc": ("cbmc", "cbmc_version"),
    "goto-cc": ("goto_cc", "goto_cc_version"),
    "goto-instrument": ("goto_instrument", "goto_instrument_version"),
}


def measured_toolchain() -> Toolchain:
    return Toolchain(
        clang=None,
        cbmc=CBMC_PATH,
        clang_version=None,
        cbmc_version=MEASURED_CBMC_VERSION,
        goto_cc=GOTO_CC_PATH,
        goto_cc_version=MEASURED_GOTO_CC_VERSION,
        goto_instrument=GOTO_INSTRUMENT_PATH,
        goto_instrument_version=MEASURED_GOTO_INSTRUMENT_VERSION,
    )


def with_tool(tc: Toolchain, tool: str, *, path: str | None, version: str | None) -> Toolchain:
    path_field, version_field = TOOL_FIELDS[tool]
    return dataclasses.replace(tc, **{path_field: path, version_field: version})


def release_line(tool: str, release: str) -> str:
    if tool == "goto-cc":
        return f"gcc (goto-cc {release} (cbmc-{release})) 13.3.0"
    return f"{release} (cbmc-{release})"
