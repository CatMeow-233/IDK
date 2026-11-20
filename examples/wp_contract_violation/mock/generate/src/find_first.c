#include "find_first.h"

int find_first(const int *a, size_t n, int key, size_t *pos)
{
    /*@ loop invariant 0 <= i <= n;
        loop invariant \forall integer k; 0 <= k < i ==> a[k] != key;
        loop assigns i;
        loop variant n - i;
    */
    for (size_t i = 0; i + 1 < n; i++) {
        if (a[i] == key) {
            *pos = i;
            return 1;
        }
    }
    return 0;
}
