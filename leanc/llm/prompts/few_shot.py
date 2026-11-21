"""Few-shot example for spec drafting: the approved safe_array_get example, embedded so no examples dir is needed."""

from __future__ import annotations

FEW_SHOT_SPEC_YAML = """\
leanc_spec: 1
spec_id: safe-array-get
version: 1
title: Safe integer array read
requirement: |
  Implement a safe integer-array read. Out-of-range indices must return an error
  instead of reading invalid memory.
language: {name: c, standard: c17}
target:
  function: safe_array_get
  signature: "int safe_array_get(const int *arr, size_t len, size_t idx, int *out)"
  params:
    - {name: arr, type: "const int *", description: "array of len readable ints, or NULL"}
    - {name: len, type: size_t, description: "number of elements in arr"}
    - {name: idx, type: size_t, description: "index to read (any value)"}
    - {name: out, type: "int *", description: "where the element is written, or NULL"}
  returns:
    type: int
    description: status code
    values:
      - {value: "SAFE_ARRAY_OK (0)", meaning: "success, *out == arr[idx]"}
      - {value: "SAFE_ARRAY_ERR_NULL (-1)", meaning: "arr or out is NULL; nothing dereferenced"}
      - {value: "SAFE_ARRAY_ERR_RANGE (-2)", meaning: "idx >= len; no memory read, *out untouched"}
contract:
  preconditions:
    - {id: PRE1, text: "if arr != NULL then arr points to at least len readable ints", c_expr: "len <= MAX_LEN"}
  postconditions:
    - {id: POST1, text: "arr, out non-NULL and idx < len => returns 0 and *out == arr[idx]"}
    - {id: POST2, text: "idx >= len => returns -2 and *out is unchanged"}
    - {id: POST3, text: "arr == NULL or out == NULL => returns -1"}
    - {id: POST4, text: "the return value is always one of 0, -1, -2"}
  safety:
    - {id: SAFE1, text: "no out-of-bounds memory access", checks: [bounds, pointer]}
    - {id: SAFE2, text: "no NULL dereference", checks: [pointer]}
    - {id: SAFE3, text: "no undefined behaviour", checks: [signed-overflow, pointer-overflow, undefined-shift]}
  frame:
    - "arr[0..len) is never modified"
sources:
  editable: [src/safe_array.c]
  headers: [include/safe_array.h]
  include_dirs: [include]
compile:
  flags: [-Wall, -Wextra, -Wpedantic]
  warnings_policy: report
static_analysis:
  policy: block
tests:
  driver: tests/test_safe_array_get.c
  cases:
    - {name: normal, args: [normal]}
    - {name: out_of_range, args: [out_of_range]}
    - {name: "null", args: ["null"]}
verification:
  harness: harness/safe_array_get_harness.c
  checks: [unsigned-overflow, conversion]
  unwind: 9
  input_bounds:
    - {name: len, min: "0", max: "8", note: "array sizes up to MAX_LEN=8 are explored"}
    - {name: idx, note: "unconstrained (full size_t range)"}
  timeout_s: 120
gates: [compile, static_analysis, sanitizer, cbmc]
repair:
  max_rounds: 5
"""

FEW_SHOT_HEADER_H = """\
#ifndef SAFE_ARRAY_H
#define SAFE_ARRAY_H
#include <stddef.h>
#define SAFE_ARRAY_OK 0
#define SAFE_ARRAY_ERR_NULL (-1)
#define SAFE_ARRAY_ERR_RANGE (-2)
int safe_array_get(const int *arr, size_t len, size_t idx, int *out);
#endif
"""

FEW_SHOT_HARNESS_C = """\
/* Trusted CBMC harness for safe_array_get (human-reviewed). */
#include <assert.h>
#include <stdlib.h>

#include "leanc.h"
#include "safe_array.h"

#define MAX_LEN 8

int main(void)
{
    int storage[MAX_LEN];
    int snapshot[MAX_LEN];
    for (size_t k = 0; k < MAX_LEN; k++) {
        storage[k] = nondet_int();
        snapshot[k] = storage[k];
    }
    size_t len = nondet_size_t();
    size_t idx = nondet_size_t();          /* idx is completely unconstrained */
    LEANC_ASSUME(len <= MAX_LEN);          /* PRE1 */
    const int *arr = nondet_bool() ? NULL : storage;
    int out_cell = nondet_int();
    int out_before = out_cell;
    int *out = nondet_bool() ? NULL : &out_cell;

    int rc = safe_array_get(arr, len, idx, out);
    LEANC_REACH("after_call");

    assert(rc == SAFE_ARRAY_OK || rc == SAFE_ARRAY_ERR_NULL || rc == SAFE_ARRAY_ERR_RANGE); /* POST4 */
    if (arr == NULL || out == NULL) {
        LEANC_REACH("null_input");
        assert(rc == SAFE_ARRAY_ERR_NULL);                                                  /* POST3 */
    } else if (idx >= len) {
        LEANC_REACH("out_of_range");
        assert(rc == SAFE_ARRAY_ERR_RANGE);                                                 /* POST2 */
        assert(out_cell == out_before);
    } else {
        LEANC_REACH("in_range");
        assert(rc == SAFE_ARRAY_OK);                                                        /* POST1 */
        assert(out_cell == storage[idx]);
    }
    for (size_t k = 0; k < MAX_LEN; k++) {
        assert(storage[k] == snapshot[k]);                                                  /* frame */
    }
    return 0;
}
"""

FEW_SHOT_TESTS_C = """\
/* Trusted runtime tests for safe_array_get (run under ASan/UBSan). */
#include <stdio.h>
#include <string.h>

#include "safe_array.h"

static int check(int cond, const char *what)
{
    if (!cond) {
        fprintf(stderr, "FAILED: %s\\n", what);
        return 1;
    }
    return 0;
}

static int test_normal(void)
{
    int a[5] = {10, 20, 30, 40, 50};
    int out = -1;
    int f = 0;
    f |= check(safe_array_get(a, 5, 0, &out) == SAFE_ARRAY_OK && out == 10, "first element");
    f |= check(safe_array_get(a, 5, 4, &out) == SAFE_ARRAY_OK && out == 50, "last element");
    return f;
}

static int test_out_of_range(void)
{
    int a[3] = {1, 2, 3};
    int out = 77;
    int f = 0;
    f |= check(safe_array_get(a, 3, 3, &out) == SAFE_ARRAY_ERR_RANGE, "idx == len");
    f |= check(safe_array_get(a, 3, (size_t)-1, &out) == SAFE_ARRAY_ERR_RANGE, "idx == SIZE_MAX");
    f |= check(safe_array_get(a, 0, 0, &out) == SAFE_ARRAY_ERR_RANGE, "empty array");
    f |= check(out == 77, "out untouched");
    return f;
}

static int test_null(void)
{
    int a[2] = {1, 2};
    int out = 0;
    int f = 0;
    f |= check(safe_array_get(NULL, 2, 0, &out) == SAFE_ARRAY_ERR_NULL, "NULL arr");
    f |= check(safe_array_get(a, 2, 0, NULL) == SAFE_ARRAY_ERR_NULL, "NULL out");
    return f;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s <case>\\n", argv[0]);
        return 2;
    }
    if (strcmp(argv[1], "normal") == 0) return test_normal();
    if (strcmp(argv[1], "out_of_range") == 0) return test_out_of_range();
    if (strcmp(argv[1], "null") == 0) return test_null();
    fprintf(stderr, "unknown case %s\\n", argv[1]);
    return 2;
}
"""
