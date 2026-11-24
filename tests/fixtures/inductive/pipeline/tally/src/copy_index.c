#include <string.h>

#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (a[i] < x) {
            c++;
        }
    }
    const unsigned char table[4] = {0, 0, 0, 0};
    unsigned char pos = 0;
    memcpy(&pos, &a[0], 1);
    return c + table[pos];
}
