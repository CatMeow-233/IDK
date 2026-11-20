#ifndef SUM_ARRAY_H
#define SUM_ARRAY_H

#include <stddef.h>

/*
 * Sum the n ints a[0..n).
 *   returns the exact mathematical sum as a long long
 *           0 if a == NULL or n == 0 (nothing is dereferenced when a == NULL)
 * The accumulator is a long long, so no overflow is possible for
 * n <= SUM_ARRAY_MAX_N (|sum| <= 2^31 * 2^20 < 2^63).
 * The array is never modified.
 */
#define SUM_ARRAY_MAX_N ((size_t)1 << 20)

long long sum_array(const int *a, size_t n);

#endif
