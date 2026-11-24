#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    static int offset = 0;
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (a[i] < x) {
            c++;
        }
    }
    return c + offset;
}
