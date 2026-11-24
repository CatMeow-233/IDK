#include <stdlib.h>

#include "tally.h"

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    int *spare = malloc(sizeof(int));
    for (int i = 0; i < n; i++) {
        if (a[i] < x) {
            c++;
        }
    }
    free(spare);
    return c;
}
