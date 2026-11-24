#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    int lo = 0;
    int hi = n;
    while (lo < hi) {
        int mid = lo + (hi - lo) / 2;
        if (a[mid] < x) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    return lo;
}
