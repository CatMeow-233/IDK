#ifndef ARRAY_READ_H
#define ARRAY_READ_H

#include <stddef.h>

/* Status codes returned by array_read. */
#define ARRAY_READ_OK 0
#define ARRAY_READ_ERR_NULL (-1)
#define ARRAY_READ_ERR_RANGE (-2)

/*
 * Read arr[idx] into *out.
 *   returns ARRAY_READ_OK        on success (*out == arr[idx])
 *           ARRAY_READ_ERR_NULL  if arr == NULL or out == NULL (nothing is dereferenced)
 *           ARRAY_READ_ERR_RANGE if idx >= len (no memory is read, *out untouched)
 * The array is never modified.
 */
int array_read(const int *arr, size_t len, size_t idx, int *out);

#endif
