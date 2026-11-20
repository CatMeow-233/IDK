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
 * The array is never modified.
 */
int safe_array_get(const int *arr, size_t len, size_t idx, int *out);

#endif
