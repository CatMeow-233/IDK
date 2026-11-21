#ifndef FIND_FIRST_H
#define FIND_FIRST_H

#include <stddef.h>

/*
 * Linear search for the FIRST occurrence of key in a[0..n).
 *   returns 1 and sets *pos to the smallest index i < n with a[i] == key, if there is one;
 *   returns 0 and leaves *pos untouched otherwise.
 * a is never modified; pos must not point into a.
 *
 * The ACSL contract below is the APPROVED formal specification used by the
 * Frama-C/WP gate (part of this protected, hash-locked header).
 */
/*@ requires \valid_read(a + (0 .. n - 1));
    requires \valid(pos);
    requires \separated(pos, a + (0 .. n - 1));
    assigns  *pos;

    behavior found:
      assumes \exists integer k; 0 <= k < n && a[k] == key;
      assigns *pos;
      ensures \result == 1;
      ensures 0 <= *pos < n && a[*pos] == key;
      ensures \forall integer k; 0 <= k < *pos ==> a[k] != key;

    behavior not_found:
      assumes \forall integer k; 0 <= k < n ==> a[k] != key;
      assigns \nothing;
      ensures \result == 0;

    complete behaviors;
    disjoint behaviors;
*/
int find_first(const int *a, size_t n, int key, size_t *pos);

#endif
