#include <string.h>

#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    int scratch[4];
    for (int i = 0; i < n; i++) {
        memset(scratch, 0, sizeof scratch);
        if (a[i] < x) {
            c++;
        }
    }
    return c;
}
