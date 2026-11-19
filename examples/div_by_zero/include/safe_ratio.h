#ifndef SAFE_RATIO_H
#define SAFE_RATIO_H

#include <limits.h>
#include <stddef.h>

/* Status codes returned by safe_ratio. */
#define SAFE_RATIO_OK 0
#define SAFE_RATIO_ERR_NULL (-1)
#define SAFE_RATIO_ERR_DIV_ZERO (-2)
#define SAFE_RATIO_ERR_OVERFLOW (-3)

/*
 * Integer quotient num / den (C semantics: truncated toward zero) into *out.
 *   returns SAFE_RATIO_OK            on success (*out == num / den)
 *           SAFE_RATIO_ERR_NULL      if out == NULL (nothing is computed or written)
 *           SAFE_RATIO_ERR_DIV_ZERO  if den == 0 (*out untouched)
 *           SAFE_RATIO_ERR_OVERFLOW  if num == INT_MIN and den == -1, whose true quotient
 *                                    INT_MAX + 1 is not representable (*out untouched)
 * The checks are applied in this order (a NULL out is reported first).
 */
int safe_ratio(int num, int den, int *out);

#endif
