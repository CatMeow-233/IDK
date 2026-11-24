#include "tally.h"

static int leanc_hook(int v) { return v; }

int tally_below(const int *a, int n, int x)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (a[i] < x) {
            c++;
        }
    }
    return c;
}

int tally_max(const int *a, int n)
{
    int best = a[0];
    int i = 1;
    while (i < n) {
        if (a[i] > best) {
            best = a[i];
        }
        i++;
    }
    return best;
}

void tally_clear(int *a, int n)
{
    for (int i = 0; i < n; i++) {
        a[i] = 0;
    }
}
