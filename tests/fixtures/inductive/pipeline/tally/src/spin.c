#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    while (n > 0) {
        c = a[0] < x;
    }
    return c;
}
