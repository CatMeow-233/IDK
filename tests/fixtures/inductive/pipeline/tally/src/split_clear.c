#include "tally.h"

void tally_clear(int *a, int n)
{
    for (int i = 0; i < n; i++) {
        a[i] = 0;
    }
}
