#include "tally.h"

static const int offset = 0;

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (a[i] < x) {
            c++;
        }
    }
    return c + offset;
}
