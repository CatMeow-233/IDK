#ifndef ARRAY_MAX_H
#define ARRAY_MAX_H

#include <stddef.h>

/*
 * Index of the FIRST maximum element of a[0..n).
 *   requires n >= 1 and a points to n readable ints;
 *   returns  i with 0 <= i < n, a[k] <= a[i] for every k < n, and a[k] < a[i] for every k < i;
 *   reads    a[0..n) only, modifies nothing.
 *
 * The ACSL contract below is the APPROVED formal specification used by the
 * Frama-C/WP gate (part of this protected, hash-locked header).
 */
/*@ requires n > 0;
    requires \valid_read(a + (0 .. n - 1));
    assigns  \nothing;
    ensures  in_bounds: 0 <= \result < n;
    ensures  is_max:    \forall integer k; 0 <= k < n ==> a[k] <= a[\result];
    ensures  is_first:  \forall integer k; 0 <= k < \result ==> a[k] < a[\result];
*/
size_t index_of_max(const int *a, size_t n);

#endif
