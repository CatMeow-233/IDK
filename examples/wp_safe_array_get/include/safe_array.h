#ifndef SAFE_ARRAY_H
#define SAFE_ARRAY_H

#include <stddef.h>

/* Status codes returned by safe_array_get. */
#define SAFE_ARRAY_OK 0
#define SAFE_ARRAY_ERR_NULL (-1)
#define SAFE_ARRAY_ERR_RANGE (-2)

/*
 * Read arr[idx] into *out.
 *   returns SAFE_ARRAY_OK        on success (*out == arr[idx])
 *           SAFE_ARRAY_ERR_NULL  if arr == NULL or out == NULL (nothing is dereferenced)
 *           SAFE_ARRAY_ERR_RANGE if idx >= len (no memory is read, *out untouched)
 * The array is never modified; out must not point into arr.
 *
 * The ACSL contract below is the APPROVED formal specification used by the
 * Frama-C/WP gate (it is part of this protected header, so it is hash-locked
 * together with the spec).
 */
/*@ requires arr == \null || \valid_read(arr + (0 .. len - 1));
    requires out == \null || \valid(out);
    requires arr != \null && out != \null ==> \separated(out, arr + (0 .. len - 1));
    assigns  *out;
    ensures  \result == SAFE_ARRAY_OK || \result == SAFE_ARRAY_ERR_NULL || \result == SAFE_ARRAY_ERR_RANGE;

    behavior null_input:
      assumes arr == \null || out == \null;
      assigns \nothing;
      ensures \result == SAFE_ARRAY_ERR_NULL;

    behavior out_of_range:
      assumes arr != \null && out != \null && idx >= len;
      assigns \nothing;
      ensures \result == SAFE_ARRAY_ERR_RANGE;
      ensures *out == \old(*out);

    behavior in_range:
      assumes arr != \null && out != \null && idx < len;
      assigns *out;
      ensures \result == SAFE_ARRAY_OK;
      ensures *out == \old(arr[idx]);

    complete behaviors;
    disjoint behaviors;
*/
int safe_array_get(const int *arr, size_t len, size_t idx, int *out);

#endif
