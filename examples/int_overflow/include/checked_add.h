#ifndef CHECKED_ADD_H
#define CHECKED_ADD_H

#include <limits.h>
#include <stddef.h>

/* Status codes returned by checked_add. */
#define CHECKED_ADD_OK 0
#define CHECKED_ADD_ERR_NULL (-1)
#define CHECKED_ADD_ERR_OVERFLOW (-2)

/*
 * Overflow-checked signed addition: *out = a + b.
 *   returns CHECKED_ADD_OK            on success (*out == a + b, mathematically exact)
 *           CHECKED_ADD_ERR_NULL      if out == NULL (nothing is computed or written)
 *           CHECKED_ADD_ERR_OVERFLOW  if a + b < INT_MIN or a + b > INT_MAX (*out untouched)
 * The check must happen BEFORE the addition: signed overflow is undefined
 * behaviour in C, so testing the result afterwards is too late.
 */
int checked_add(int a, int b, int *out);

#endif
