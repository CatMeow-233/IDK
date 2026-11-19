#ifndef MUL_CHECKED_H
#define MUL_CHECKED_H

#include <stdint.h>

/* Status codes returned by mul_u64_checked. */
#define MULCHK_OK 0
#define MULCHK_ERR_NULL (-1)      /* out == NULL; nothing computed */
#define MULCHK_ERR_OVERFLOW (-2)  /* a * b does not fit in 64 bits; *out untouched */

/*
 * Overflow-checked 64-bit unsigned multiplication.
 *   returns MULCHK_OK and *out == a * b   if the mathematical product fits in uint64_t
 *           MULCHK_ERR_OVERFLOW            otherwise (*out untouched)
 *           MULCHK_ERR_NULL                if out == NULL
 */
int mul_u64_checked(uint64_t a, uint64_t b, uint64_t *out);

#endif
