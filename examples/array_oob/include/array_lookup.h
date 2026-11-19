#ifndef ARRAY_LOOKUP_H
#define ARRAY_LOOKUP_H

#include <stddef.h>

/* Status codes returned by array_lookup. */
#define ARRAY_LOOKUP_OK 0
#define ARRAY_LOOKUP_ERR_NULL (-1)
#define ARRAY_LOOKUP_ERR_RANGE (-2)

/*
 * Copy values[index] into *out.
 *   returns ARRAY_LOOKUP_OK         on success (*out == values[index])
 *           ARRAY_LOOKUP_ERR_NULL   if values == NULL or out == NULL (nothing is dereferenced)
 *           ARRAY_LOOKUP_ERR_RANGE  if index >= count (no memory is read, *out untouched)
 * values must point to at least count readable ints; it is never modified.
 */
int array_lookup(const int *values, size_t count, size_t index, int *out);

#endif
